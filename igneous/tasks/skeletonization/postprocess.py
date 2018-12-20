from collections import defaultdict

import networkx as nx
import numpy as np

from scipy import spatial
from scipy.sparse import lil_matrix
from scipy.sparse.csgraph import dijkstra
import scipy.sparse.csgraph as csgraph

from cloudvolume.lib import Bbox
from cloudvolume import PrecomputedSkeleton

import igneous.skeletontricks

## Public API of Module

def trim_skeleton(skeleton, dust_threshold=4000, tick_threshold=6000):
  skeleton = remove_dust(skeleton, dust_threshold) 
  skeleton = remove_loops(skeleton)
  skeleton = connect_pieces(skeleton)
  skeleton = remove_ticks(skeleton, tick_threshold)
  return skeleton

## Implementation Details Below

def combination_pairs(n):
  pairs = np.array([])

  for i in range(n):
    for j in range(n-i-1):
      pairs = np.concatenate((pairs, np.array([i, i+j+1 ])))

  pairs = np.reshape(pairs,[ pairs.shape[0] // 2, 2 ])
  return pairs.astype(np.uint16)

def find_connected(nodes, edges):
  s = nodes.shape[0] 
  nodes = np.unique(edges).astype(np.uint32)

  conn_mat = lil_matrix((s, s), dtype=np.bool)
  conn_mat[edges[:,0], edges[:,1]] = 1

  n, l = csgraph.connected_components(conn_mat, directed=False)
  
  l_nodes = l[nodes]
  l_list = np.unique(l_nodes)
  return [ l == i for i in l_list  ]

def remove_dust(skeleton, dust_threshold):
  """Dust threshold in physical cable length."""
  
  if skeleton.empty():
    return skeleton

  skels = [] 
  for skel in skeleton.components():
    if skel.cable_length() > dust_threshold:
      skels.append(skel)

  skeleton = PrecomputedSkeleton.simple_merge(skels)
  return skeleton.consolidate()

def connect_pieces(skeleton):
  if skeleton.empty():
    return skeleton

  nodes = skeleton.vertices
  edges = skeleton.edges
  radii = skeleton.radii

  all_connected = True
  while all_connected:
    connected = find_connected(nodes, edges)
    pairs = combination_pairs(len(connected))

    all_connected = False
    for i in range(pairs.shape[0]):
      path_piece = connected[pairs[i,0]]
      nodes_piece = nodes[path_piece].astype(np.float32)
      nodes_piece_idx = np.where(path_piece)[0]

      path_tree = connected[pairs[i,1]]
      nodes_tree = nodes[path_tree]
      nodes_tree_idx = np.where(path_tree)[0]
      tree = spatial.cKDTree(nodes_tree)

      (dist, idx) = tree.query(nodes_piece)
      min_dist = np.min(dist)

      min_dist_idx = int(np.where(dist == min_dist)[0][0])
      start_idx = nodes_piece_idx[min_dist_idx]
      end_idx = nodes_tree_idx[idx[min_dist_idx]]

      # test if line between points exits object
      if (radii[start_idx] + radii[end_idx]) >= min_dist:
        new_edge = np.array([[ start_idx, end_idx ]])
        edges = np.concatenate((edges, new_edge), axis=0)
        all_connected = True
        break

  skeleton.edges = edges
  return skeleton.consolidate()

def remove_ticks(skeleton, threshold):
  """
  Simple merging of individual TESAR cubes results in lots of little 
  ticks due to the edge effect. We can remove them by thresholding
  the path length from a given branch to the "main body" of the neurite. 
  We successively remove paths from shortest to longest until no branches
  below threshold remain.

  If TEASAR parameters were chosen such that they allowed for spines to
  be traced, this is also an opportunity to correct for that.

  This algorithm is O(N^2) in the number of terminal nodes.

  Parameters:
    threshold: The maximum length in nanometers that may be culled.

  Returns: tick free skeleton
  """
  if skeleton.empty():
    return skeleton

  skels = []
  for component in skeleton.components():
    skels.append(_remove_ticks(component, threshold))

  return PrecomputedSkeleton.simple_merge(skels).consolidate()

def _remove_ticks(skeleton, threshold):
  """
  For a single connected component, remove "ticks" below a threshold. 
  Ticks are a path connecting a terminal node to a branch point that
  are physically shorter than the specified threshold. 

  Every time a tick is removed, it potentially changes the topology
  of the components. Once a branch point's number of edges drops to
  two, the two paths connecting to it can be unified into one. Sometimes
  a single object exists that has no branches but is below threshold. We
  do not delete these objects as there would be nothing left.

  Each time the minimum length tick is removed, it can change which 
  tick is the new minimum tick and requires reevaluation of the whole 
  skeleton. Previously, we did not perform this reevaluation and it 
  resulted in the ends of neurites being clipped. 

  This makes the algorithm quadratic in the number of terminal branches.
  As high resolution skeletons can have tens of thousands of nodes and 
  dozens of branches, a full topological reevaluation becomes relatively 
  expensive. However, we only need to know the graph of distances between
  critical points, defined as the set of branch points and terminal points, 
  in the skeleton in order to evaluate the topology. 

  Therefore, we first compute this distance graph before proceeding with
  tick removal. The algorithm remains quadratic in the number of terminal
  points, but the constant speed up is very large as we move from a regime
  of tens of thousands to hundreds of thousands of points needing reevaluation
  to at most hundreds and often only a handful in typical cases. In the 
  pathological case of a skeleton with numerous single point extrusions,
  the performance of the algorithm collapses approximately to the previous
  regime (though without the assistence of the constant factor of numpy speed).

  Requires:
    skeleton: a PrecomputedSkeleton that is guaranteed to be a single 
      connected component.
    threshold: distance in nanometers below which a branch is considered
      a "tick" eligible to be removed.

  Returns: a "tick" free PrecomputedSkeleton
  """
  if skeleton.empty():
    return skeleton

  dgraph = _create_distance_graph(skeleton)
  vertices = skeleton.vertices
  edges = skeleton.edges

  unique_nodes, unique_counts = np.unique(edges, return_counts=True)
  terminal_nodes = set(unique_nodes[ unique_counts == 1 ])

  branch_idx = np.where(unique_counts >= 3)[0]

  branch_counts = defaultdict(int)
  for i in branch_idx:
    branch_counts[unique_nodes[i]] = unique_counts[i]

  G = nx.Graph()
  G.add_edges_from(edges)

  def fuse_edge(edg1):
    unify = [ edg for edg in dgraph.keys() if edg1 in edg ]
    new_dist = 0.0
    for edg in unify:
      new_dist += dgraph[edg]
      del dgraph[edg]
    unify = set([ item for sublist in unify for item in sublist ])
    unify.remove(edg1)
    dgraph[tuple(unify)] = new_dist
    branch_counts[edg1] = 0

  while len(dgraph) > 1:
    terminal_superedges = [ edg for edg in dgraph.keys() if (edg[0] in terminal_nodes or edg[1] in terminal_nodes) ]
    min_edge = min(terminal_superedges, key=dgraph.get)
    e1, e2 = min_edge

    if branch_counts[e1] == 1 and branch_counts[e2] == 1:
      break
    elif dgraph[min_edge] >= threshold:
      break

    path = nx.shortest_path(G, e1, e2)
    path = [ (path[i], path[i+1]) for i in range(len(path) - 1) ]
    G.remove_edges_from(path)

    del dgraph[min_edge]
    branch_counts[e1] -= 1
    branch_counts[e2] -= 1

    if branch_counts[e1] == 2:
      fuse_edge(e1)
    if branch_counts[e2] == 2:
      fuse_edge(e2)

  skel = skeleton.clone()
  skel.edges = np.array(list(G.edges), dtype=np.uint32)
  return skel.consolidate()

def _create_distance_graph(skeleton):
  """
  Creates the distance "supergraph" from a single connected component 
  skeleton as described in _remove_ticks.

  Returns: a distance "supergraph" describing the physical distance
    between the critical points in the skeleton's structure.

  Example skeleton with output:

      60nm   60nm   60nm     
    1------2------3------4
      30nm |  70nm \
           5        ----6

  { 
    (1,2): 60,  
    (2,3): 60,
    (2,5): 30,
    (3,4): 60,
    (3,6): 70,
  }
  """
  vertices = skeleton.vertices
  edges = skeleton.edges

  unique_nodes, unique_counts = np.unique(edges, return_counts=True)
  terminal_nodes = unique_nodes[ unique_counts == 1 ]
  branch_nodes = set(unique_nodes[ unique_counts >= 3 ])
  
  critical_points = set(terminal_nodes)
  critical_points.update(branch_nodes)

  tree = defaultdict(set)

  for e1, e2 in edges:
    tree[e1].add(e2)
    tree[e2].add(e1)

  # The below depth first search would be
  # more elegantly implemented as recursion,
  # but it quickly blows the stack, mandating
  # an iterative implementation.

  stack = [ terminal_nodes[0] ]
  parents = [ -1 ]
  dist_stack = [ 0.0 ]
  root_stack = [ terminal_nodes[0] ]
  distgraph = defaultdict(float) # the distance "supergraph"

  while stack:
    node = stack.pop()
    dist = dist_stack.pop()
    root = root_stack.pop()
    parent = parents.pop()

    if node in critical_points and node != root:
      distgraph[ (root, node) ] = dist
      dist = 0.0
      root = node

    for child in tree[node]:
      if child != parent:
        stack.append(child)
        parents.append(node)
        dist_stack.append(
          dist + np.linalg.norm(vertices[node,:] - vertices[child,:])
        )
        root_stack.append(root)

  return distgraph

def remove_loops(skeleton):
  if skeleton.empty():
    return skeleton

  skels = []
  for component in skeleton.components():
    skels.append(_remove_loops(component))

  return PrecomputedSkeleton.simple_merge(skels).consolidate()

def _remove_loops(skeleton):
  nodes = skeleton.vertices
  G = nx.Graph()

  # Double bookeeping to prevent having to 
  # (expensively) call:
  #
  # edges = np.array(list(G.edges), dtype=np.int32)
  #
  # which effectively doubled the running time. The
  # iterator of that function is just slow.

  G.add_edges_from(skeleton.edges)
  edges = np.copy(skeleton.edges).astype(np.int32)

  num_verts = skeleton.vertices.shape[0]

  while True: # Loop until all cycles are removed
    edges = edges.astype(np.int32)
    edges_cycle = igneous.skeletontricks.find_cycle(edges, num_verts)

    if len(edges_cycle) == 0:
      break

    edges_cycle = np.array(edges_cycle, dtype=np.uint32)
    edges_cycle = np.sort(edges_cycle, axis=1)

    nodes_cycle = np.unique(edges_cycle)
    nodes_cycle = nodes_cycle.astype(np.int32)
    
    unique_nodes, unique_counts = np.unique(edges, return_counts=True)
    branch_nodes = unique_nodes[ unique_counts >= 3 ]

    # branch cycles are cycle nodes that coincide with a branch point
    branch_cycle = nodes_cycle[np.isin(nodes_cycle,branch_nodes)]
    branch_cycle = branch_cycle.astype(np.int32)

    if branch_cycle.shape[0] == 1:
      branch_cycle_point = nodes[branch_cycle, :]
      cycle_points = nodes[nodes_cycle, :]

      dist = np.sum((cycle_points - branch_cycle_point) ** 2, 1)
      end_node = nodes_cycle[np.argmax(dist)]

      G.remove_edges_from(edges_cycle)
      G.add_edge(branch_cycle[0], end_node)

      edges = remove_row(edges, edges_cycle)        
      new_edge = np.array([[branch_cycle[0], end_node]], dtype=np.int32) 
      edges = np.concatenate((edges, new_edge), 0)

    elif branch_cycle.shape[0] == 2:
      path = nx.shortest_path(G, branch_cycle[0], branch_cycle[1])

      edge_path = path2edge(path)
      edge_path = np.sort(edge_path, axis=1)

      row_valid = np.ones(edges_cycle.shape[0])
      for i in range(edge_path.shape[0]):
        row_valid -= (edges_cycle[:,0] == edge_path[i,0]) * (edges_cycle[:,1] == edge_path[i,1])

      row_valid = row_valid.astype(np.bool)
      edge_path = edges_cycle[row_valid,:]

      G.remove_edges_from(edge_path)
      edges = remove_row(edges, edge_path)

    elif branch_cycle.shape[0] == 0:
      G.remove_edges_from(edges_cycle)
      edges = remove_row(edges, edges_cycle)

    else:
      branch_cycle_points = nodes[branch_cycle,:]

      centroid = np.mean(branch_cycle_points, axis=0)
      dist = np.sum((nodes - centroid) ** 2, 1)
      intersect_node = np.argmin(dist)
      intersect_point = nodes[intersect_node,:]

      G.remove_edges_from(edges_cycle)
      edges = remove_row(edges, edges_cycle)      

      new_edges = np.zeros((branch_cycle.shape[0], 2))
      new_edges[:,0] = branch_cycle
      new_edges[:,1] = intersect_node

      if np.isin(intersect_node, branch_cycle):
        idx = np.where(branch_cycle == intersect_node)
        new_edges = np.delete(new_edges, idx, 0)

      G.add_edges_from(new_edges)
      edges = np.concatenate((edges,new_edges), 0)

  skeleton.vertices = nodes
  skeleton.edges = edges.astype(np.uint32)
  return skeleton

def path2edge(path):
  """
  path: sequence of nodes

  Returns: sequence separated into edges
  """
  edges = np.zeros([len(path) - 1, 2], dtype=np.uint32)
  for i in range(len(path)-1):
    edges[i,0] = path[i]
    edges[i,1] = path[i+1]
  return edges

def remove_row(array, rows2remove): 
  array = np.sort(array, axis=1)  
  rows2remove = np.sort(rows2remove, axis=1)  

  for i in range(rows2remove.shape[0]):  
    idx = find_row(array,rows2remove[i,:])  
    if np.sum(idx == -1) == 0: 
      array = np.delete(array, idx, axis=0) 
  
  return array.astype(np.int32)


def find_row(array, row): 
  """ 
  array: array to search for  
  row: row to find  
   Returns: row indices 
  """ 
  row = np.array(row) 
  if array.shape[1] != row.size: 
    raise ValueError("Dimensions do not match!")  
    
  NDIM = array.shape[1] 
  valid = np.zeros(array.shape, dtype=np.bool) 
  for i in range(NDIM):  
    valid[:,i] = array[:,i] == row[i] 
  
  row_loc = np.zeros([ array.shape[0], 1 ])  
  if NDIM == 2:  
    row_loc = valid[:,0] * valid[:,1] 
  elif NDIM == 3: 
    row_loc = valid[:,0] * valid[:,1] * valid[:,2]  
  
  idx = np.where(row_loc==1)[0]  
  if len(idx) == 0: 
    idx = -1  
  return idx 
