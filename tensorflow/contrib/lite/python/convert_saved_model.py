# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Functions to convert SavedModel to frozen GraphDefs."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.contrib.saved_model.python.saved_model import reader
from tensorflow.contrib.saved_model.python.saved_model import signature_def_utils
from tensorflow.core.framework import types_pb2
from tensorflow.python.client import session
from tensorflow.python.framework import graph_util as tf_graph_util
from tensorflow.python.framework import ops
from tensorflow.python.platform import gfile
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.saved_model import loader
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import tag_constants


def _write_and_flush_file(file_path, data_str):
  """Writes data to file path.

  Args:
    file_path: Full path of the file to store data in.
    data_str: Data represented as a string.

  Returns: None.
  """
  with gfile.Open(file_path, "wb") as data_file:
    data_file.write(data_str)
    data_file.flush()


def _log_tensor_details(tensor_info):
  """Log tensor details: name, shape, and type."""
  for key in tensor_info:
    val = tensor_info[key]
    dtype = types_pb2.DataType.Name(val.dtype)
    if val.tensor_shape.unknown_rank:
      shape = "unknown_rank"
    else:
      dims = [str(dim.size) for dim in val.tensor_shape.dim]
      shape = "({})".format(", ".join(dims))

    logging.info("Tensor's key in saved_model's tensor_map: %s", key)
    logging.info(" tensor name: %s, shape: %s, type: %s", val.name, shape,
                 dtype)


def _get_meta_graph_def(saved_model_dir, tag_set):
  """Validate saved_model and extract MetaGraphDef.

  Args:
    saved_model_dir: saved_model path to convert.
    tag_set: Set of tag(s) of the MetaGraphDef to load.

  Returns:
    The meta_graph_def used for tflite conversion.

  Raises:
    ValueError: No valid MetaGraphDef for given tag_set.
  """
  saved_model = reader.read_saved_model(saved_model_dir)
  tag_sets = []
  result_meta_graph_def = None
  for meta_graph_def in saved_model.meta_graphs:
    meta_graph_tag_set = set(meta_graph_def.meta_info_def.tags)
    tag_sets.append(meta_graph_tag_set)
    if meta_graph_tag_set == tag_set:
      result_meta_graph_def = meta_graph_def
  logging.info("The given saved_model contains the following tags: %s",
               tag_sets)
  if result_meta_graph_def is not None:
    return result_meta_graph_def
  else:
    raise ValueError("No valid MetaGraphDef for this tag_set '{}'. Possible "
                     "values are '{}'. ".format(tag_set, tag_sets))


def _get_signature_def(meta_graph, signature_key):
  """Get the signature def from meta_graph with given signature_key.

  Args:
    meta_graph: meta_graph_def.
    signature_key: signature_def in the meta_graph_def.

  Returns:
    The signature_def used for tflite conversion.

  Raises:
    ValueError: Given signature_key is not valid for this meta_graph.
  """
  signature_def_map = meta_graph.signature_def
  signature_def_keys = set(signature_def_map.keys())
  logging.info(
      "The given SavedModel MetaGraphDef contains SignatureDefs with the "
      "following keys: %s", signature_def_keys)
  if signature_key not in signature_def_keys:
    raise ValueError("No '{}' in the SavedModel\'s SignatureDefs. Possible "
                     "values are '{}'.".format(signature_key,
                                               ",".join(signature_def_keys)))
  signature_def = signature_def_utils.get_signature_def_by_key(
      meta_graph, signature_key)
  return signature_def


def _get_inputs_outputs(signature_def):
  """Get inputs and outputs from SignatureDef.

  Args:
    signature_def: SignatureDef in the meta_graph_def for conversion.

  Returns:
    The inputs and outputs in the graph for conversion.
  """
  inputs_tensor_info = signature_def.inputs
  outputs_tensor_info = signature_def.outputs
  logging.info("input tensors info: ")
  _log_tensor_details(inputs_tensor_info)
  logging.info("output tensors info: ")
  _log_tensor_details(outputs_tensor_info)

  def gather_names(tensor_info):
    return [tensor_info[key].name for key in tensor_info]

  inputs = gather_names(inputs_tensor_info)
  outputs = gather_names(outputs_tensor_info)
  return inputs, outputs


def _get_tensors(graph, signature_def_tensor_names=None,
                 user_tensor_names=None):
  """Gets the tensors associated with the tensor names.

  Either signature_def_tensor_names or user_tensor_names should be provided. If
  the user provides tensors, the tensors associated with the user provided
  tensor names are provided. Otherwise, the tensors associated with the names in
  the SignatureDef are provided.

  Args:
    graph: GraphDef representing graph.
    signature_def_tensor_names: Tensor names stored in either the inputs or
      outputs of a SignatureDef. (default None)
    user_tensor_names: Tensor names provided by the user. (default None)

  Returns:
    List of tensors.

  Raises:
    ValueError:
      signature_def_tensors and user_tensor_names are undefined or empty.
      user_tensor_names are not valid.
  """
  tensors = []
  if user_tensor_names:
    # Get the list of all of the tensors with and without the tensor index.
    all_tensor_names = [
        tensor.name for op in graph.get_operations() for tensor in op.outputs
    ]
    all_tensor_names_only = [name.split(":")[0] for name in all_tensor_names]

    # Sort the tensor names.
    user_tensor_names = sorted(user_tensor_names)

    # Get the tensors associated with the tensor names.
    tensors = []
    invalid_tensors = []
    for name in user_tensor_names:
      if name not in all_tensor_names_only:
        invalid_tensors.append(name)
      else:
        idx = all_tensor_names_only.index(name)
        tensors.append(graph.get_tensor_by_name(all_tensor_names[idx]))

    # Throw ValueError if any user input names are not valid tensors.
    if invalid_tensors:
      raise ValueError("Invalid tensors '{}' were found.".format(
          ",".join(invalid_tensors)))
  elif signature_def_tensor_names:
    tensors = [
        graph.get_tensor_by_name(name)
        for name in sorted(signature_def_tensor_names)
    ]
  else:
    # Throw ValueError if signature_def_tensors and user_tensor_names are both
    # either undefined or empty.
    raise ValueError(
        "Specify either signature_def_tensor_names or user_tensor_names")

  return tensors


def freeze_saved_model(saved_model_dir, input_arrays, input_shapes,
                       output_arrays, tag_set, signature_key):
  """Converts a SavedModel to a frozen graph.

  Args:
    saved_model_dir: SavedModel directory to convert.
    input_arrays: List of input tensors to freeze graph with. Uses input arrays
      from SignatureDef when none are provided. (default None)
    input_shapes: Map of strings representing input tensor names to list of
      integers representing input shapes (e.g., {"foo": : [1, 16, 16, 3]}).
      Automatically determined when input shapes is None (e.g., {"foo" : None}).
      (default None)
    output_arrays: List of output tensors to freeze graph with. Uses output
      arrays from SignatureDef when none are provided. (default None)
    tag_set: Set of tags identifying the MetaGraphDef within the SavedModel to
      analyze. All tags in the tag set must be present. (default "serve")
    signature_key: Key identifying SignatureDef containing inputs and outputs.

  Returns:
    frozen_graph_def: Frozen GraphDef.
    in_tensors: List of input tensors for the graph.
    out_tensors: List of output tensors for the graph.

  Raises:
    ValueError:
      SavedModel doesn't contain a MetaGraphDef identified by tag_set.
      signature_key is not in the MetaGraphDef.
      input_shapes does not match the length of input_arrays.
      input_arrays or output_arrays are not valid.
      Unable to load Session.
  """
  # Set default values for inputs if they are set to None.
  if signature_key is None:
    signature_key = signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY
  if tag_set is None:
    tag_set = set([tag_constants.SERVING])

  # Read SignatureDef.
  meta_graph = _get_meta_graph_def(saved_model_dir, tag_set)
  signature_def = _get_signature_def(meta_graph, signature_key)
  inputs, outputs = _get_inputs_outputs(signature_def)

  graph = ops.Graph()
  with session.Session(graph=graph) as sess:
    # TODO(nupurgarg): Throw ValueError if SavedModel has assets/ directory.
    loader.load(sess, meta_graph.meta_info_def.tags, saved_model_dir)

    # Gets input and output tensors.
    # TODO(zhixianyan): Use TFLite supported Op list to filter outputs.
    in_tensors = _get_tensors(graph, inputs, input_arrays)
    out_tensors = _get_tensors(graph, outputs, output_arrays)

    # Gets fully defined tensor shape.
    for tensor in in_tensors:
      if (input_shapes and tensor.name in input_shapes and
          input_shapes[tensor.name] is not None):
        shape = input_shapes[tensor.name]
      else:
        shape = tensor.get_shape().as_list()
      tensor.set_shape(shape)

    output_names = [node.split(":")[0] for node in outputs]
    frozen_graph_def = tf_graph_util.convert_variables_to_constants(
        sess, graph.as_graph_def(), output_names)

    return frozen_graph_def, in_tensors, out_tensors
  raise ValueError("Unable to load Session.")
