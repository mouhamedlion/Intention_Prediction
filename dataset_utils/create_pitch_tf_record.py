"""Converts Pitch2d data to TFRecords file format with Example protos.

The raw Pitch2d data set is expected to reside in PNG files located in the
following directory structure.

  data_dir/intent##/ID/datetime_trial##/trial_datetime_frame_####.png
  e.g.: /data_dir/intent02/ZL/201801302254_trial08/trial_201801302254_frame0045.png
  ...

where 'intent##' is the unique label associated with
these images.

The training data set consists of 9 sub-directories (i.e. labels)
each containing 1080 PNG images for a total of 9720 PNG images.

The evaluation data set consists of 9 sub-directories (i.e. labels)
each containing 380 PNG images for a total of 3420 PNG images.

The testing data set consists of 9 sub-directories (i.e. labels)
each containing 380 PNG images for a total of 3420 PNG images.

This TensorFlow script converts the training, evaluation and testing data into
a sharded data set consisting of 16, 8 and 8 TFRecord files, respectively.

  train_directory/train-00-of-16
  train_directory/train-01-of-16
  ...
  train_directory/train-16-of-16

and

  validation_directory/validation-00-of-08
  validation_directory/validation-01-of-08
  ...
  validation_directory/validation-08-of-08

and

  test_directory/test-00-of-08
  test_directory/test-01-of-08
  ...
  test_directory/test-08-of-08


Each training TFRecord file contains ~608 records. Each validation TFRecord 
file contains ~428 records. Each testing TFRecord file contains ~428 records. 
Each record within the TFRecord file is a serialized Example proto. 
The Example proto contains the following fields:

  image/encoded: string containing PNG encoded image in RGB colorspace
  image/height: integer, image height in pixels
  image/width: integer, image width in pixels
  image/colorspace: string, specifying the colorspace, always 'RGB'
  image/channels: integer, specifying the number of channels, always 3
  image/format: string, specifying the format, always 'JPEG' or 'PNG'

  image/filename: string containing the basename of the image file
            e.g. 'trial_201801302254_frame0045.png'
  image/class/label: integer specifying the index in a classification layer.
    The label ranges from [1, 9] where 0 is not used.
  image/class/pid: string specifying the unique ID of the pitcher,
    e.g. 'ZL'

Running this script using 8 threads may take around ~? hours 
on an Alienware Aurora R6.

Running this script using 16 threads may take around ~? hours 
on an Threadripper 1900X + ASRock X399 Taichi.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os
import random
import sys
import threading

import numpy as np
import six
import tensorflow as tf

tf.app.flags.DEFINE_string('train_directory', '/tmp/',
                           'Training data directory')
tf.app.flags.DEFINE_string('validation_directory', '/tmp/',
                           'Validation data directory')
tf.app.flags.DEFINE_string('test_directory', '/tmp/',
                           'Test data directory')
tf.app.flags.DEFINE_string('output_directory', '/tmp/',
                           'Output data directory')

tf.app.flags.DEFINE_integer('train_shards', 1024,
                            'Number of shards in training TFRecord files.')
tf.app.flags.DEFINE_integer('validation_shards', 128,
                            'Number of shards in validation TFRecord files.')

tf.app.flags.DEFINE_integer('num_threads', 8,
                            'Number of threads to preprocess the images.')

# The labels file contains a list of valid labels are held in this file.
# Assumes that the file contains entries as such:
#   n01440764
#   n01443537
#   n01484850
# where each line corresponds to a label expressed as a synset. We map
# each synset contained in the file to an integer (based on the alphabetical
# ordering). See below for details.
tf.app.flags.DEFINE_string('labels_file',
                           'imagenet_lsvrc_2015_synsets.txt',
                           'Labels file')

# This file containing mapping from synset to human-readable label.
# Assumes each line of the file looks like:
#
#   n02119247    black fox
#   n02119359    silver fox
#   n02119477    red fox, Vulpes fulva
#
# where each line corresponds to a unique mapping. Note that each line is
# formatted as <synset>\t<human readable label>.
tf.app.flags.DEFINE_string('imagenet_metadata_file',
                           'imagenet_metadata.txt',
                           'ImageNet metadata file')

# This file is the output of process_bounding_box.py
# Assumes each line of the file looks like:
#
#   n00007846_64193.JPEG,0.0060,0.2620,0.7545,0.9940
#
# where each line corresponds to one bounding box annotation associated
# with an image. Each line can be parsed as:
#
#   <JPEG file name>, <xmin>, <ymin>, <xmax>, <ymax>
#
# Note that there might exist mulitple bounding box annotations associated
# with an image file.
tf.app.flags.DEFINE_string('bounding_box_file',
                           './imagenet_2012_bounding_boxes.csv',
                           'Bounding box file')

FLAGS = tf.app.flags.FLAGS


def _int64_feature(value):
  """Wrapper for inserting int64 features into Example proto."""
  if not isinstance(value, list):
    value = [value]
  return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _float_feature(value):
  """Wrapper for inserting float features into Example proto."""
  if not isinstance(value, list):
    value = [value]
  return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def _bytes_feature(value):
  """Wrapper for inserting bytes features into Example proto."""
  if isinstance(value, six.string_types):           
    value = six.binary_type(value, encoding='utf-8') 
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def _convert_to_example(filename, image_buffer, label, synset, human, bbox,
                        height, width):
  """Build an Example proto for an example.

  Args:
    filename: string, path to an image file, e.g., '/path/to/example.JPG'
    image_buffer: string, JPEG encoding of RGB image
    label: integer, identifier for the ground truth for the network
    synset: string, unique WordNet ID specifying the label, e.g., 'n02323233'
    human: string, human-readable label, e.g., 'red fox, Vulpes vulpes'
    bbox: list of bounding boxes; each box is a list of integers
      specifying [xmin, ymin, xmax, ymax]. All boxes are assumed to belong to
      the same label as the image label.
    height: integer, image height in pixels
    width: integer, image width in pixels
  Returns:
    Example proto
  """
  xmin = []
  ymin = []
  xmax = []
  ymax = []
  for b in bbox:
    assert len(b) == 4
    # pylint: disable=expression-not-assigned
    [l.append(point) for l, point in zip([xmin, ymin, xmax, ymax], b)]
    # pylint: enable=expression-not-assigned

  colorspace = 'RGB'
  channels = 3
  image_format = 'JPEG'

  example = tf.train.Example(features=tf.train.Features(feature={
      'image/height': _int64_feature(height),
      'image/width': _int64_feature(width),
      'image/colorspace': _bytes_feature(colorspace),
      'image/channels': _int64_feature(channels),
      'image/class/label': _int64_feature(label),
      'image/class/synset': _bytes_feature(synset),
      'image/class/text': _bytes_feature(human),
      'image/object/bbox/xmin': _float_feature(xmin),
      'image/object/bbox/xmax': _float_feature(xmax),
      'image/object/bbox/ymin': _float_feature(ymin),
      'image/object/bbox/ymax': _float_feature(ymax),
      'image/object/bbox/label': _int64_feature([label] * len(xmin)),
      'image/format': _bytes_feature(image_format),
      'image/filename': _bytes_feature(os.path.basename(filename)),
      'image/encoded': _bytes_feature(image_buffer)}))
  return example


class ImageCoder(object):
  """Helper class that provides TensorFlow image coding utilities."""

  def __init__(self):
    # Create a single Session to run all image coding calls.
    self._sess = tf.Session()

    # Initializes function that converts PNG to JPEG data.
    self._png_data = tf.placeholder(dtype=tf.string)
    image = tf.image.decode_png(self._png_data, channels=3)
    self._png_to_jpeg = tf.image.encode_jpeg(image, format='rgb', quality=100)

    # Initializes function that converts CMYK JPEG data to RGB JPEG data.
    self._cmyk_data = tf.placeholder(dtype=tf.string)
    image = tf.image.decode_jpeg(self._cmyk_data, channels=0)
    self._cmyk_to_rgb = tf.image.encode_jpeg(image, format='rgb', quality=100)

    # Initializes function that decodes RGB JPEG data.
    self._decode_jpeg_data = tf.placeholder(dtype=tf.string)
    self._decode_jpeg = tf.image.decode_jpeg(self._decode_jpeg_data, channels=3)

  def png_to_jpeg(self, image_data):
    return self._sess.run(self._png_to_jpeg,
                          feed_dict={self._png_data: image_data})

  def cmyk_to_rgb(self, image_data):
    return self._sess.run(self._cmyk_to_rgb,
                          feed_dict={self._cmyk_data: image_data})

  def decode_jpeg(self, image_data):
    image = self._sess.run(self._decode_jpeg,
                           feed_dict={self._decode_jpeg_data: image_data})
    assert len(image.shape) == 3
    assert image.shape[2] == 3
    return image


def _is_png(filename):
  """Determine if a file contains a PNG format image.

  Args:
    filename: string, path of the image file.

  Returns:
    boolean indicating if the image is a PNG.
  """
  # File list from:
  # https://groups.google.com/forum/embed/?place=forum/torch7#!topic/torch7/fOSTXHIESSU
  return 'n02105855_2933.JPEG' in filename


def _is_cmyk(filename):
  """Determine if file contains a CMYK JPEG format image.

  Args:
    filename: string, path of the image file.

  Returns:
    boolean indicating if the image is a JPEG encoded with CMYK color space.
  """
  # File list from:
  # https://github.com/cytsai/ilsvrc-cmyk-image-list
  blacklist = ['n01739381_1309.JPEG', 'n02077923_14822.JPEG',
               'n02447366_23489.JPEG', 'n02492035_15739.JPEG',
               'n02747177_10752.JPEG', 'n03018349_4028.JPEG',
               'n03062245_4620.JPEG', 'n03347037_9675.JPEG',
               'n03467068_12171.JPEG', 'n03529860_11437.JPEG',
               'n03544143_17228.JPEG', 'n03633091_5218.JPEG',
               'n03710637_5125.JPEG', 'n03961711_5286.JPEG',
               'n04033995_2932.JPEG', 'n04258138_17003.JPEG',
               'n04264628_27969.JPEG', 'n04336792_7448.JPEG',
               'n04371774_5854.JPEG', 'n04596742_4225.JPEG',
               'n07583066_647.JPEG', 'n13037406_4650.JPEG']
  return filename.split('/')[-1] in blacklist


def _process_image(filename, coder):
  """Process a single image file.

  Args:
    filename: string, path to an image file e.g., '/path/to/example.JPG'.
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
  Returns:
    image_buffer: string, JPEG encoding of RGB image.
    height: integer, image height in pixels.
    width: integer, image width in pixels.
  """
  # Read the image file.
  with tf.gfile.FastGFile(filename, 'rb') as f:
    image_data = f.read()

  # Clean the dirty data.
  if _is_png(filename):
    # 1 image is a PNG.
    print('Converting PNG to JPEG for %s' % filename)
    image_data = coder.png_to_jpeg(image_data)
  elif _is_cmyk(filename):
    # 22 JPEG images are in CMYK colorspace.
    print('Converting CMYK to RGB for %s' % filename)
    image_data = coder.cmyk_to_rgb(image_data)

  # Decode the RGB JPEG.
  image = coder.decode_jpeg(image_data)

  # Check that image converted to RGB
  assert len(image.shape) == 3
  height = image.shape[0]
  width = image.shape[1]
  assert image.shape[2] == 3

  return image_data, height, width


def _process_image_files_batch(coder, thread_index, ranges, name, filenames,
                               synsets, labels, humans, bboxes, num_shards):
  """Processes and saves list of images as TFRecord in 1 thread.

  Args:
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
    thread_index: integer, unique batch to run index is within [0, len(ranges)).
    ranges: list of pairs of integers specifying ranges of each batches to
      analyze in parallel.
    name: string, unique identifier specifying the data set
    filenames: list of strings; each string is a path to an image file
    synsets: list of strings; each string is a unique WordNet ID
    labels: list of integer; each integer identifies the ground truth
    humans: list of strings; each string is a human-readable label
    bboxes: list of bounding boxes for each image. Note that each entry in this
      list might contain from 0+ entries corresponding to the number of bounding
      box annotations for the image.
    num_shards: integer number of shards for this data set.
  """
  # Each thread produces N shards where N = int(num_shards / num_threads).
  # For instance, if num_shards = 128, and the num_threads = 2, then the first
  # thread would produce shards [0, 64).
  num_threads = len(ranges)
  assert not num_shards % num_threads
  num_shards_per_batch = int(num_shards / num_threads)

  shard_ranges = np.linspace(ranges[thread_index][0],
                             ranges[thread_index][1],
                             num_shards_per_batch + 1).astype(int)
  num_files_in_thread = ranges[thread_index][1] - ranges[thread_index][0]

  counter = 0
  for s in range(num_shards_per_batch):
    # Generate a sharded version of the file name, e.g. 'train-00002-of-00010'
    shard = thread_index * num_shards_per_batch + s
    output_filename = '%s-%.5d-of-%.5d' % (name, shard, num_shards)
    output_file = os.path.join(FLAGS.output_directory, output_filename)
    writer = tf.python_io.TFRecordWriter(output_file)

    shard_counter = 0
    files_in_shard = np.arange(shard_ranges[s], shard_ranges[s + 1], dtype=int)
    for i in files_in_shard:
      filename = filenames[i]
      label = labels[i]
      synset = synsets[i]
      human = humans[i]
      bbox = bboxes[i]

      image_buffer, height, width = _process_image(filename, coder)

      example = _convert_to_example(filename, image_buffer, label,
                                    synset, human, bbox,
                                    height, width)
      writer.write(example.SerializeToString())
      shard_counter += 1
      counter += 1

      if not counter % 1000:
        print('%s [thread %d]: Processed %d of %d images in thread batch.' %
              (datetime.now(), thread_index, counter, num_files_in_thread))
        sys.stdout.flush()

    writer.close()
    print('%s [thread %d]: Wrote %d images to %s' %
          (datetime.now(), thread_index, shard_counter, output_file))
    sys.stdout.flush()
    shard_counter = 0
  print('%s [thread %d]: Wrote %d images to %d shards.' %
        (datetime.now(), thread_index, counter, num_files_in_thread))
  sys.stdout.flush()


def _process_image_files(name, filenames, synsets, labels, humans,
                         bboxes, num_shards):
  """Process and save list of images as TFRecord of Example protos.

  Args:
    name: string, unique identifier specifying the data set
    filenames: list of strings; each string is a path to an image file
    synsets: list of strings; each string is a unique WordNet ID
    labels: list of integer; each integer identifies the ground truth
    humans: list of strings; each string is a human-readable label
    bboxes: list of bounding boxes for each image. Note that each entry in this
      list might contain from 0+ entries corresponding to the number of bounding
      box annotations for the image.
    num_shards: integer number of shards for this data set.
  """
  assert len(filenames) == len(synsets)
  assert len(filenames) == len(labels)
  assert len(filenames) == len(humans)
  assert len(filenames) == len(bboxes)

  # Break all images into batches with a [ranges[i][0], ranges[i][1]].
  spacing = np.linspace(0, len(filenames), FLAGS.num_threads + 1).astype(np.int)
  ranges = []
  threads = []
  for i in range(len(spacing) - 1):
    ranges.append([spacing[i], spacing[i + 1]])

  # Launch a thread for each batch.
  print('Launching %d threads for spacings: %s' % (FLAGS.num_threads, ranges))
  sys.stdout.flush()

  # Create a mechanism for monitoring when all threads are finished.
  coord = tf.train.Coordinator()

  # Create a generic TensorFlow-based utility for converting all image codings.
  coder = ImageCoder()

  threads = []
  for thread_index in range(len(ranges)):
    args = (coder, thread_index, ranges, name, filenames,
            synsets, labels, humans, bboxes, num_shards)
    t = threading.Thread(target=_process_image_files_batch, args=args)
    t.start()
    threads.append(t)

  # Wait for all the threads to terminate.
  coord.join(threads)
  print('%s: Finished writing all %d images in data set.' %
        (datetime.now(), len(filenames)))
  sys.stdout.flush()


def _find_image_files(data_dir, labels):
  """Build a list of all images files and labels in the data set.

  Args:
    data_dir: string, path to the root directory of images.

      Assumes that the ImageNet data set resides in PNG files located in
      the following directory structure.

        data_dir/intent##/ID/datetime_trial##/trial_datetime_frame_####.png

    labels: list of labels grab from .

      We start the integer labels at 1 is to reserve label 0 as an
      unused background class.

  Returns:
    filenames: list of strings; each string is a path to an image file.
    labels: list of integer; each integer identifies the ground truth.
  """
  print('Determining list of input files and labels from %s.' % data_dir)
  # Prepare training, validation and test data 
  train_paths = []
  val_paths = []
  test_paths = []

  train_labels = []
  val_labels = []
  test_labels = []

  intents = []
  labels = []
  filenames = []

  # Leave label index 0 empty as a background class.
  label_index = 1

  # Construct the list of PNG files and labels.
  intent_paths = glob.glob(data_dir+'/*')
  for ipath in intent_paths:
    intent = ipath.split('/')[-1]
    intents.append(intent)
    labels.append(label_index)

    pitcher_paths = glob.glob(ipath+'/*')
    for ppath in pitcher_paths:
      trial_paths = glob.glob(ppath+'/*')
      np.random.shuffle(trial_paths) # shuffle all 10 trials, before travaltes arrangement
      #separate images to train, val, test (travaltes), 6/2/2
      train_trial_paths = trial_paths[:int(0.6*len(trial_paths))]
      val_trial_paths = trial_paths[int(0.6*len(trial_paths)):int(0.8*len(trial_paths))]
      test_trial_paths = trial_paths[int(0.8*len(trial_paths)):]
      for trntrlpath in train_trial_paths:
        train_img_paths = glob.glob(trntrlpath+'/*.png')
        train_paths += train_img_paths
        train_labels += [label_index] * len(train_img_paths)
        for valtrlpath in val_trial_paths:
          val_img_paths = glob.glob(valtrlpath+'/*.png')
          val_labels += val_img_paths
          val_labels += [label_index] * len(val_img_paths)
          for testrlpath in test_trial_paths:
            test_img_paths = glob.glob(testrlpath+'/*.png')
            test_labels += test_img_paths
            test_labels += [label_index] * len(test_img_paths)

    # Construct the list of PNG files and labels
    print('Finished finding files in %d of %d classes.' % (intent))
    label_index += 1 # label index increase when investigating new intent

  # Shuffle the ordering of all image files in order to guarantee
  # random ordering of the images with respect to label in the
  # saved TFRecord files. Make the randomization repeatable.
  shuffled_index = list(range(len(filenames)))
  random.seed(12345)
  random.shuffle(shuffled_index)

  filenames = [filenames[i] for i in shuffled_index]
  synsets = [synsets[i] for i in shuffled_index]
  labels = [labels[i] for i in shuffled_index]

  print('Found %d JPEG files across %d labels inside %s.' %
        (len(filenames), len(challenge_synsets), data_dir))
  return filenames, synsets, labels


def _process_dataset(name, directory, num_shards):
  """Process a complete data set and save it as a TFRecord.

  Args:
    name: string, unique identifier specifying the data set.
    directory: string, root path to the data set.
    num_shards: integer number of shards for this data set.
  """
  filenames, labels = _find_image_files(directory, FLAGS.labels_file)
  _process_image_files(name, filenames, labels, num_shards)

def main(unused_argv):
  assert not FLAGS.train_shards % FLAGS.num_threads, (
      'Please make the FLAGS.num_threads commensurate with FLAGS.train_shards')
  assert not FLAGS.validation_shards % FLAGS.num_threads, (
      'Please make the FLAGS.num_threads commensurate with '
      'FLAGS.validation_shards')
  assert not FLAGS.test_shards % FLAGS.num_threads, (
      'Please make the FLAGS.num_threads commensurate with '
      'FLAGS.test_shards')
  print('Saving results to %s' % FLAGS.output_directory)

  # Run it!
  _process_dataset('validation', FLAGS.validation_directory,
                   FLAGS.validation_shards, synset_to_human, image_to_bboxes)
  _process_dataset('train', FLAGS.train_directory, FLAGS.train_shards,
                   synset_to_human, image_to_bboxes)
  _process_dataset('test', FLAGS.test_directory, FLAGS.test_shards, pitcher_id)


if __name__ == '__main__':
  tf.app.run()
