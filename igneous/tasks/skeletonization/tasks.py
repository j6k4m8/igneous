try:
    from StringIO import cStringIO as BytesIO
except ImportError:
    from io import BytesIO

from functools import reduce
import json
import pickle
import os
import re
from collections import defaultdict

import numpy as np
import scipy.ndimage
from tqdm import tqdm

from cloudvolume import CloudVolume, PrecomputedSkeleton
from cloudvolume.storage import Storage, SimpleStorage
from cloudvolume.lib import xyzrange, min2, max2, Vec, Bbox, mkdir, save_images

import edt # euclidean distance transform
import fastremap
from taskqueue import RegisteredTask

from igneous import chunks, downsample, downsample_scales
import igneous.cc3d as cc3d
import igneous.skeletontricks

from .skeletonization import TEASAR
from .postprocess import (
  crop_skeleton, merge_skeletons, trim_skeleton,
  consolidate_skeleton, simple_merge_skeletons
)

def skeldir(cloudpath):
  with SimpleStorage(cloudpath) as storage:
    info = storage.get_json('info')

  skel_dir = 'skeletons/'
  if 'skeletons' in info:
    skel_dir = info['skeletons']
  return skel_dir


class SkeletonTask(RegisteredTask):
  """
  Stage 1 of skeletonization.

  Convert chunks of segmentation into chunked skeletons and point clouds.
  They will be merged in the stage 2 task SkeletonMergeTask.
  """
  def __init__(self, cloudpath, shape, offset, mip, teasar_params, crop_zone, will_postprocess, info=None, object_ids=None):
    super(SkeletonTask, self).__init__(cloudpath, shape, offset, mip, teasar_params, crop_zone, will_postprocess)
    self.cloudpath = cloudpath
    self.bounds = Bbox(offset, Vec(*shape) + Vec(*offset))
    self.mip = mip
    self.teasar_params = teasar_params
    self.crop_zone = crop_zone
    self.will_postprocess = will_postprocess
    self.cloudinfo = info
    self.object_ids = object_ids

  def execute(self):
    vol = CloudVolume(self.cloudpath, mip=self.mip, cache=True, info=self.cloudinfo, cdn_cache=False)
    bbox = Bbox.clamp(self.bounds, vol.bounds)

    all_labels = vol[ bbox.to_slices() ]
    all_labels = all_labels[:,:,:,0]
    if self.object_ids is not None:
      all_labels[~np.isin(all_labels,self.object_ids)] = 0

    tmp_labels, remapping = fastremap.renumber(all_labels)
    cc_labels = cc3d.connected_components(tmp_labels, max_labels=int(bbox.volume() / 4))
    del tmp_labels
    remapping = igneous.skeletontricks.get_mapping(all_labels, cc_labels)
    del all_labels

    path = skeldir(self.cloudpath)
    path = os.path.join(self.cloudpath, path)

    all_dbf = edt.edt(
      np.ascontiguousarray(cc_labels), 
      anisotropy=vol.resolution.tolist(),
      black_border=False,
    )
    all_dbf = np.asfortranarray(all_dbf)

    cc_segids, pxct = np.unique(cc_labels, return_counts=True)
    cc_segids = [ sid for sid, ct in zip(cc_segids, pxct) if ct > 1000 ]

    all_slices = scipy.ndimage.find_objects(cc_labels)

    skeletons = defaultdict(list)
    for segid in tqdm(cc_segids):
      if segid == 0:
        continue 

      # Crop DBF to ROI
      slices = all_slices[segid - 1]
      if slices is None:
        continue

      labels = cc_labels[slices]
      labels = (labels == segid)
      dbf = labels * all_dbf[slices]

      roi = Bbox.from_slices(slices)
      roi += bbox.minpt 

      skeleton = self.skeletonize(labels, dbf, roi, anisotropy=vol.resolution.tolist())

      if skeleton.empty():
        continue

      orig_segid = remapping[segid]
      skeleton.id = orig_segid
      skeleton.vertices *= vol.resolution
      skeletons[orig_segid].append( (skeleton, roi) )

    self.upload(vol, path, skeletons)
      
  def upload(self, vol, path, skeletons):
    if not self.will_postprocess:
      skels = []
      for segid, skel_roi in skeletons.items():
        skel = [ skel for skel, roi in skel_roi ]
        skel = reduce(simple_merge_skeletons, skel)
        skels.append(skel)
      vol.skeleton.upload_multiple(skels)
    else:
      with Storage(path, progress=True) as stor:
        for segid, skel_roi in skeletons.items():
          for skeleton, roi in skel_roi:
            stor.put_file(
              file_path="{}:{}".format(skeleton.id, roi.to_filename()),
              content=pickle.dumps(skeleton),
              compress='gzip',
              content_type="application/python-pickle",
              cache_control=False,
            )

      
  def skeletonize(self, labels, dbf, bbox, anisotropy):
    skeleton = TEASAR(labels, dbf, anisotropy=anisotropy, **self.teasar_params)

    skeleton.vertices[:,0] += bbox.minpt.x
    skeleton.vertices[:,1] += bbox.minpt.y
    skeleton.vertices[:,2] += bbox.minpt.z

    # Crop by 50px to avoid edge effects.
    crop_bbox = bbox.clone()
    crop_bbox.minpt += self.crop_zone
    crop_bbox.maxpt -= self.crop_zone

    if crop_bbox.volume() <= 0:
      return skeleton

    return crop_skeleton(skeleton, crop_bbox)

class SkeletonMergeTask(RegisteredTask):
  """
  Stage 2 of skeletonization.

  Merge chunked TEASAR skeletons into a single skeleton.

  If we parallelize using prefixes single digit prefixes ['0','1',..'9'] all meshes will
  be correctly processed. But if we do ['10','11',..'99'] meshes from [0,9] won't get
  processed and need to be handle specifically by creating tasks that will process
  a single mesh ['0:','1:',..'9:']
  """
  def __init__(self, cloudpath, prefix):
    super(SkeletonMergeTask, self).__init__(cloudpath, prefix)
    self.cloudpath = cloudpath
    self.prefix = prefix

  def execute(self):
    self.vol = CloudVolume(self.cloudpath)

    with Storage(self.cloudpath) as stor:
      skels = self.get_filenames_subset(stor)

      vol = self.vol

      for segid, frags in tqdm(skels.items()):
        skeleton = self.fuse_skeletons(frags, stor)
        skeleton = trim_skeleton(skeleton)
        vol.skeleton.upload(segid, skeleton.vertices, skeleton.edges, skeleton.radii)
      
      for segid, frags in skels.items():
        stor.delete_files(frags)

  def get_filenames_subset(self, storage):
    prefix = '{}/{}'.format(self.vol.skeleton.path, self.prefix)
    skeletons = defaultdict(list)

    for filename in storage.list_files(prefix=prefix):
      # `match` implies the beginning (^). `search` matches whole string
      matches = re.search(r'(\d+):', filename)

      if not matches:
        continue

      segid, = matches.groups()
      segid = int(segid)
      skeletons[segid].append(filename)

    return skeletons

  def fuse_skeletons(self, filenames, storage):
    if len(filenames) == 0:
      return Skeleton()
    
    skldl = storage.get_files(filenames)
    skeletons = { item['filename'] : pickle.loads(item['content']) for item in skldl }

    if len(skeletons) == 1:
      return skeletons[filenames[0]]

    file_pairs = self.find_paired_skeletons(filenames)

    for fname1, fname2 in file_pairs:
      skel1, skel2 = skeletons[fname1], skeletons[fname2]
      skel1, skel2 = merge_skeletons(skel1, skel2)
      skeletons[fname1] = skel1
      skeletons[fname2] = skel2

    skeletons = list(skeletons.values())
    skeletons = [ s for s in skeletons if not s.empty() ]

    if len(skeletons) == 0:
      return PrecomputedSkeleton()

    fusing = skeletons[0]
    offset = 0
    for skel in skeletons[1:]:
      if skel.edges.shape[0] == 0:                                                                                                                                                                                                                                                                                                                                                            
        continue

      skel.edges = skel.edges.astype(np.uint32)
      skel.edges += offset
      offset += skel.vertices.shape[0]

      fusing.vertices = np.concatenate((fusing.vertices, skel.vertices), axis=0)
      fusing.edges = np.concatenate((fusing.edges, skel.edges), axis=0)
      fusing.radii = np.concatenate((fusing.radii, skel.radii), axis=0)

    return consolidate_skeleton(fusing)

  def find_paired_skeletons(self, filenames):
    pairs = []

    for i in range(len(filenames) - 1):
      adj1 = Bbox.from_filename(filenames[i])
      for j in range(i + 1, len(filenames)):
        adj2 = Bbox.from_filename(filenames[j])

        # We're testing for overlap, tasks
        # are created with 50% overlap
        if Bbox.intersects(adj1, adj2):
          pairs.append(
            (filenames[i], filenames[j])
          )

    return pairs







