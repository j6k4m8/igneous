import multiprocessing as mp
import os

import click
from taskqueue import TaskQueue
from taskqueue.lib import toabs
from taskqueue.paths import get_protocol

from igneous import task_creation as tc
from igneous.secrets import LEASE_SECONDS, SQS_REGION_NAME

def normalize_path(queuepath):
  if not get_protocol(queuepath):
    return "fq://" + toabs(queuepath)
  return queuepath

@click.group()
@click.option("-p", "--parallel", default=1, help="Run with this number of parallel processes. If 0, use number of cores.")
@click.version_option(version="0.1.0")
@click.pass_context
def main(ctx, parallel):
  """
  CLI tool for managing igneous jobs.
  
  Igneous is a tool for producing neuroglancer
  datasets. It scales to hundreds of teravoxels
  or more.

  https://github.com/seung-lab/igneous

  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version. Run "igneous license" for details.
  """
  parallel = int(parallel)
  if parallel == 0:
    parallel = mp.cpu_count()
  ctx.ensure_object(dict)
  ctx.obj["parallel"] = max(min(parallel, mp.cpu_count()), 1)

@main.command()
def license():
  """Prints the license for this library and cli tool."""
  path = os.path.join(os.path.dirname(__file__), 'LICENSE')
  with open(path, 'rt') as f:
    print(f.read())

@main.command()
@click.argument("path")
@click.option('--queue', default=None, required=True, help="AWS SQS queue or directory to be used for a task queue. e.g. sqs://my-queue or ./my-queue. See https://github.com/seung-lab/python-task-queue")
@click.option('--mip', default=0, help="Build upward from this level of the image pyramid. Default: 0")
@click.option('--fill-missing', is_flag=True, default=False, help="Interpret missing image files as background instead of failing.")
@click.option('--num-mips', default=5, help="Build this many additional pyramid levels. Each increment increases memory requirements per task 4-8x.  Default: 5")
@click.option('--cseg', is_flag=True, default=False, help="Use the compressed_segmentation image chunk encoding scheme. Segmentation only.")
@click.option('--sparse', is_flag=True, default=False, help="Don't count black pixels in mode or average calculations. For images, eliminates edge ghosting in 2x2x2 downsample. For segmentation, prevents small objects from disappearing at high mip levels.")
@click.option('--chunk-size', default=None, help="Chunk size of new layers. e.g. 128,128,64")
@click.option('--compress', default=None, help="Set the image compression scheme. Options: 'gzip', 'br'")
@click.option('--volumetric', is_flag=True, default=False, help="Use 2x2x2 downsampling.")
@click.option('--delete-bg', is_flag=True, default=False, help="Issue a delete instead of uploading a background tile. This is helpful on systems that don't like tiny files.")
@click.option('--bg-color', default=0, help="Determines which color is regarded as background. Default: 0")
@click.pass_context
def downsample(
	ctx, path, queue, mip, fill_missing, 
	num_mips, cseg, sparse, 
	chunk_size, compress, volumetric,
	delete_bg, bg_color
):
  """
  Create an image pyramid for grayscale or labeled images.
  By default, we use 2x2x1 downsampling. 

  The levels of the pyramid are called "mips" (from the fake latin 
  "Multum in Parvo" or "many in small"). The base of the pyramid,
  the highest resolution layer, is mip 0. Each level of the pyramid
  is one mip level higher.

  The general strategy is to downsample starting from mip 0. This
  builds several levels. Once that job is complete, pass in the 
  current top mip level of the pyramid. This builds it even taller
  (referred to as "superdownsampling").
  """
  encoding = ("compressed_segmentation" if cseg else None)
  factor = (2,2,1)
  if volumetric:
  	factor = (2,2,2)

  tasks = tc.create_downsampling_tasks(
    path, mip=mip, fill_missing=fill_missing, 
    num_mips=num_mips, sparse=sparse, 
    chunk_size=chunk_size, encoding=encoding, 
    delete_black_uploads=delete_bg, 
    background_color=bg_color, 
    compress=compress,
    factor=factor  	
  )

  parallel = int(ctx.obj.get("parallel", 1))
  tq = TaskQueue(normalize_path(queue))
  tq.insert(tasks, parallel=parallel)

@main.command()
@click.argument("src")
@click.argument("dest")
@click.option('--queue', default=None, required=True, help="AWS SQS queue or directory to be used for a task queue. e.g. sqs://my-queue or ./my-queue. See https://github.com/seung-lab/python-task-queue")
@click.option('--mip', default=0, help="Build upward from this level of the image pyramid. Default: 0")
@click.option('--translate', default="0,0,0", help="Translate the bounding box by X,Y,Z voxels in the new location.")
@click.option('--downsample/--skip-downsample', is_flag=True, default=True, help="Whether or not to produce downsamples from transfer tiles. Default: True")
@click.option('--fill-missing', is_flag=True, default=False, help="Interpret missing image files as background instead of failing.")
@click.option('--num-mips', default=5, help="Build this many additional pyramid levels. Each increment increases memory requirements per task 4-8x.  Default: 5")
@click.option('--cseg', is_flag=True, default=False, help="Use the compressed_segmentation image chunk encoding scheme. Segmentation only.")
@click.option('--sparse', is_flag=True, default=False, help="Don't count black pixels in mode or average calculations. For images, eliminates edge ghosting in 2x2x2 downsample. For segmentation, prevents small objects from disappearing at high mip levels.")
@click.option('--shape', default="2048,2048,64", help="Set the task shape. This also determines how many downsamples you get. e.g. 2048,2048,64")
@click.option('--chunk-size', default=None, help="Chunk size of new layers. e.g. 128,128,64")
@click.option('--compress', default=None, help="Set the image compression scheme. Options: 'gzip', 'br'")
@click.option('--volumetric', is_flag=True, default=False, help="Use 2x2x2 downsampling.")
@click.option('--delete-bg', is_flag=True, default=False, help="Issue a delete instead of uploading a background tile. This is helpful on systems that don't like tiny files.")
@click.option('--bg-color', default=0, help="Determines which color is regarded as background. Default: 0")
@click.pass_context
def xfer(
	ctx, src, dest, queue, translate, downsample, mip, 
	fill_missing, num_mips, cseg, shape, sparse, 
	chunk_size, compress, volumetric,
	delete_bg, bg_color
):
  """Transfer an image layer to another location."""
  encoding = ("compressed_segmentation" if cseg else None)
  factor = (2,2,1)
  if volumetric:
  	factor = (2,2,2)

  shape = [ int(axis) for axis in shape.split(",") ]
  translate = [ int(amt) for amt in translate.split(",") ]

  tasks = tc.create_transfer_tasks(
    src, dest, 
    chunk_size=chunk_size, fill_missing=fill_missing, 
    translate=translate, mip=mip, shape=shape,
    encoding=encoding, skip_downsamples=(not downsample),
    delete_black_uploads=delete_bg, background_color=bg_color,
    compress=compress, factor=factor, sparse=sparse,
  )

  parallel = int(ctx.obj.get("parallel", 1))
  tq = TaskQueue(normalize_path(queue))
  tq.insert(tasks, parallel=parallel)

@main.command()
@click.argument("queue", type=str)
@click.option('--aws-region', default=SQS_REGION_NAME, help=f"AWS region in which the SQS queue resides. Default: {SQS_REGION_NAME}")
@click.option('--lease-sec', default=LEASE_SECONDS, help=f"Seconds to lease a task for. Default: {LEASE_SECONDS}", type=int)
@click.option('--tally/--no-tally', is_flag=True, default=True, help="Tally completed fq tasks. Does not apply to SQS.")
@click.option('--min-sec', default=-1, help='Execute for at least this many seconds and quit after the last task finishes. Special values: (0) Run at most a single task. (-1) Loop forever (default).', type=float)
@click.pass_context
def execute(ctx, queue, aws_region, lease_sec, tally, min_sec):
  """Execute igneous tasks from a queue.

  The queue must be an AWS SQS queue or a FileQueue directory. 
  Examples: (SQS) sqs://my-queue (FileQueue) fq://./my-queue
  (the fq:// is optional).

  See https://github.com/seung-lab/python-task-queue
  """
  parallel = int(ctx.obj.get("parallel", 1))
  args = (queue, aws_region, lease_sec, tally, min_sec)

  if parallel == 1:
    execute_helper(*args)
    return

  pool = mp.Pool(processes=parallel)
  try:
    for _ in range(parallel):
      pool.apply_async(execute_helper, args)
    pool.close()
    pool.join()
  except KeyboardInterrupt:
    print("Interrupted. Exiting.")
    pool.terminate()
    pool.join()

def execute_helper(queue, aws_region, lease_sec, tally, min_sec):
  tq = TaskQueue(normalize_path(queue), region_name=aws_region)

  def stop_after_elapsed_time(elapsed_time):
    if min_sec < 0:
      return False
    return min_sec < elapsed_time

  if min_sec != 0:
    tq.poll(
      lease_seconds=lease_sec,
      verbose=True,
      tally=tally,
      stop_fn=stop_after_elapsed_time,
    )
  else:
    task = tq.lease(seconds=lease_sec)
    task.execute()


