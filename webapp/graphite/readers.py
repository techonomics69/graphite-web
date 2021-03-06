import os
import sys
import time
# Use the built-in version of scandir/stat if possible, otherwise
# use the scandir module version
try:
    from os import scandir, stat # noqa # pylint: disable=unused-import
except ImportError:
    from scandir import scandir, stat # noqa # pylint: disable=unused-import

from graphite.intervals import Interval, IntervalSet
from graphite.carbonlink import CarbonLink
from graphite.logger import log
from django.conf import settings

try:
  import whisper
except ImportError:
  whisper = False

# The parser was repalcing __readHeader with the <class>__readHeader
# which was not working.
if bool(whisper):
  whisper__readHeader = whisper.__readHeader

try:
  import ceres
except ImportError:
  ceres = False

try:
  import rrdtool
except ImportError:
  rrdtool = False

try:
  import gzip
except ImportError:
  gzip = False


class FetchInProgress(object):
  def __init__(self, wait_callback):
    self.wait_callback = wait_callback

  def waitForResults(self):
    return self.wait_callback()


class MultiReader(object):
  __slots__ = ('nodes',)

  def __init__(self, nodes):
    self.nodes = nodes

  def get_intervals(self):
    interval_sets = []
    for node in self.nodes:
      interval_sets.extend( node.intervals.intervals )
    return IntervalSet( sorted(interval_sets) )

  def fetch(self, startTime, endTime, now=None, requestContext=None):
    # Start the fetch on each node
    fetches = []

    for n in self.nodes:
      try:
        fetches.append(n.fetch(startTime, endTime, now, requestContext))
      except:
        log.exception("Failed to initiate subfetch for %s" % str(n))

    def merge_results():
      results = {}

      # Wait for any asynchronous operations to complete
      for i, result in enumerate(fetches):
        if isinstance(result, FetchInProgress):
          try:
            results[i] = result.waitForResults()
          except:
            log.exception("Failed to complete subfetch")
            results[i] = None

      results = [r for r in results.values() if r is not None]
      if not results:
        raise Exception("All sub-fetches failed")

      return reduce(self.merge, results)

    return FetchInProgress(merge_results)

  def merge(self, results1, results2):
    # Ensure results1 is finer than results2
    if results1[0][2] > results2[0][2]:
      results1, results2 = results2, results1

    time_info1, values1 = results1
    time_info2, values2 = results2
    start1, end1, step1 = time_info1
    start2, end2, step2 = time_info2

    step   = step1                # finest step
    start  = min(start1, start2)  # earliest start
    end    = max(end1, end2)      # latest end
    time_info = (start, end, step)
    values = []

    t = start
    while t < end:
      # Look for the finer precision value first if available
      i1 = (t - start1) / step1

      if len(values1) > i1:
        v1 = values1[i1]
      else:
        v1 = None

      if v1 is None:
        i2 = (t - start2) / step2

        if len(values2) > i2:
          v2 = values2[i2]
        else:
          v2 = None

        values.append(v2)
      else:
        values.append(v1)

      t += step

    return (time_info, values)


class CeresReader(object):
  __slots__ = ('ceres_node', 'real_metric_path')
  supported = bool(ceres)

  def __init__(self, ceres_node, real_metric_path):
    self.ceres_node = ceres_node
    self.real_metric_path = real_metric_path

  def get_intervals(self):
    intervals = []
    for info in self.ceres_node.slice_info:
      (start, end, step) = info
      intervals.append( Interval(start, end) )

    return IntervalSet(intervals)

  def fetch(self, startTime, endTime):
    data = self.ceres_node.read(startTime, endTime)
    time_info = (data.startTime, data.endTime, data.timeStep)
    values = list(data.values)

    # Merge in data from carbon's cache
    try:
      cached_datapoints = CarbonLink.query(self.real_metric_path)
    except:
      log.exception("Failed CarbonLink query '%s'" % self.real_metric_path)
      cached_datapoints = []

    values = merge_with_cache(cached_datapoints,
                              data.startTime,
                              data.timeStep,
                              values)

    return time_info, values


class WhisperReader(object):
  __slots__ = ('fs_path', 'real_metric_path')
  supported = bool(whisper)

  def __init__(self, fs_path, real_metric_path):
    self.fs_path = fs_path
    self.real_metric_path = real_metric_path

  def get_intervals(self):
    start = time.time() - whisper.info(self.fs_path)['maxRetention']
    end = max( stat(self.fs_path).st_mtime, start )
    return IntervalSet( [Interval(start, end)] )

  def fetch(self, startTime, endTime, now=None, requestContext=None):
    try:
      data = whisper.fetch(self.fs_path, startTime, endTime, now)
    except IOError:
      log.exception("Failed fetch of whisper file '%s'" % self.fs_path)
      return None
    if not data:
      return None

    time_info, values = data
    (start,end,step) = time_info

    meta_info = whisper.info(self.fs_path)
    aggregation_method = meta_info['aggregationMethod']
    # Merge in data from carbon's cache
    cached_datapoints = []
    try:
      cached_datapoints = CarbonLink.query(self.real_metric_path)
    except:
      log.exception("Failed CarbonLink query '%s'" % self.real_metric_path)
      cached_datapoints = []

    if isinstance(cached_datapoints, dict):
      cached_datapoints = cached_datapoints.items()

    values = merge_with_cache(cached_datapoints,
                              start,
                              step,
                              values,
                              aggregation_method)

    return time_info, values


class CarbonCacheReader(object):
  __slots__ = ('metric')
  supported = True

  def __init__(self, metric):
    self.metric = metric

  def get_intervals(self):
    # intervals doesn't matter in such type of reader
    # Let's return time.time()
    start = time.time()
    end = start
    return IntervalSet( [Interval(start, end)] )

  def _format_and_extract_time(self, start_time, end_time, max_retention):
    """
    This function is design for formatting and extracting from
    and until time.
    """
    now = int(time.time())
    oldest_time = now - max_retention

    # Some checks
    if end_time is None:
      end_time = now
    if start_time is None:
      return None

    from_time = int(start_time)
    until_time = int(end_time)

    # Compare with now
    if from_time > now:
      return None
    if until_time > now:
      until_time = now

    # Compare with oldest_time
    if from_time < oldest_time:
      from_time = oldest_time
    if until_time < oldest_time:
      return None

    return (from_time, until_time)

  def _calculate_step(self, archives, diff):
    target_arch = None
    for archive in archives:
      retention = archive[0] * archive[1]
      if retention >= diff:
        target_arch = archive
        break
    if not target_arch:
      return None
    step = target_arch[0]
    return step

  def _query_and_format_cache_data(self, from_time, until_time, step):
    cached_results = CarbonLink.query(self.metric)
    if cached_results:
      from_interval = int(from_time - (from_time % step)) + step
      until_interval = int(until_time - (until_time % step)) + step
      if from_interval == until_interval:
        until_interval += step
      points = (until_interval - from_interval) // step
      values = [None] * points
      time_info = (from_interval, until_interval, step)
      for (timestamp, value) in cached_results:
        interval = int(timestamp - (timestamp % step))
        index = (interval - from_interval) / step
        if index < 0 or index >= points:
          continue
        values[index] = value
      return time_info, values

  def fetch(self, start_time, end_time):
    # Fetch data from carbon cache through CarbonLink
    schema = CarbonLink.get_storage_schema(self.metric)
    archives = schema["archives"]
    # Get lowest step
    lowest_step = min([arch[0] for arch in archives])

    now = int(time.time())
    max_retention = max([arch[0] * arch[1] for arch in archives])
    oldest_time = now - max_retention

    # format and extract from/until time
    from_and_until_time = self._format_and_extract_time(start_time, end_time, max_retention)
    if not from_and_until_time:
      return None
    from_time, until_time = from_and_until_time

    # calcucate step
    diff = now - from_time
    # sorted_archives = sorted(archives, key=lambda x: x[0] * x[1])
    step = self._calculate_step(archives, diff)
    if not step:
      return None

    # Only check carbon-cache if step == lowest_step
    if step == lowest_step:
      return self._query_and_format_cache_data(from_time, until_time, step)
    return None


class GzippedWhisperReader(WhisperReader):
  supported = bool(whisper and gzip)

  def get_intervals(self):
    fh = gzip.GzipFile(self.fs_path, 'rb')
    try:
      info = whisper__readHeader(fh) # evil, but necessary.
    finally:
      fh.close()

    start = time.time() - info['maxRetention']
    end = max( stat(self.fs_path).st_mtime, start )
    return IntervalSet( [Interval(start, end)] )

  def fetch(self, startTime, endTime):
    fh = gzip.GzipFile(self.fs_path, 'rb')
    try:
      return whisper.file_fetch(fh, startTime, endTime)
    finally:
      fh.close()


class RRDReader:
  supported = bool(rrdtool)

  @staticmethod
  def _convert_fs_path(fs_path):
    if isinstance(fs_path, unicode):
      fs_path = fs_path.encode(sys.getfilesystemencoding())
    return os.path.realpath(fs_path)

  def __init__(self, fs_path, datasource_name):
    self.fs_path = RRDReader._convert_fs_path(fs_path)
    self.datasource_name = datasource_name

  def get_intervals(self):
    start = time.time() - self.get_retention(self.fs_path)
    end = max( stat(self.fs_path).st_mtime, start )
    return IntervalSet( [Interval(start, end)] )

  def fetch(self, startTime, endTime):
    startString = time.strftime("%H:%M_%Y%m%d+%Ss", time.localtime(startTime))
    endString = time.strftime("%H:%M_%Y%m%d+%Ss", time.localtime(endTime))

    if settings.FLUSHRRDCACHED:
      rrdtool.flushcached(self.fs_path, '--daemon', settings.FLUSHRRDCACHED)

    (timeInfo, columns, rows) = rrdtool.fetch(self.fs_path,settings.RRD_CF,'-s' + startString,'-e' + endString)
    colIndex = list(columns).index(self.datasource_name)
    rows.pop() #chop off the latest value because RRD returns crazy last values sometimes
    values = (row[colIndex] for row in rows)

    return (timeInfo, values)

  @staticmethod
  def get_datasources(fs_path):
    info = rrdtool.info(RRDReader._convert_fs_path(fs_path))

    if 'ds' in info:
      return [datasource_name for datasource_name in info['ds']]
    else:
      ds_keys = [ key for key in info if key.startswith('ds[') ]
      datasources = set( key[3:].split(']')[0] for key in ds_keys )
      return list(datasources)

  @staticmethod
  def get_retention(fs_path):
    info = rrdtool.info(RRDReader._convert_fs_path(fs_path))
    if 'rra' in info:
      rras = info['rra']
    else:
      # Ugh, I like the old python-rrdtool api better..
      rra_count = max([ int(key[4]) for key in info if key.startswith('rra[') ]) + 1
      rras = [{}] * rra_count
      for i in range(rra_count):
        rras[i]['pdp_per_row'] = info['rra[%d].pdp_per_row' % i]
        rras[i]['rows'] = info['rra[%d].rows' % i]

    retention_points = 0
    for rra in rras:
      points = rra['pdp_per_row'] * rra['rows']
      if points > retention_points:
        retention_points = points

    return  retention_points * info['step']


def merge_with_cache(cached_datapoints, start, step, values, func=None):

  consolidated=[]

  # Similar to the function in render/datalib:TimeSeries
  def consolidate(func, values):
      usable = [v for v in values if v is not None]
      if not usable: return None
      if func == 'sum':
          return sum(usable)
      if func == 'average':
          return float(sum(usable)) / len(usable)
      if func == 'max':
          return max(usable)
      if func == 'min':
          return min(usable)
      if func == 'last':
          return usable[-1]
      raise Exception("Invalid consolidation function: '%s'" % func)

  if func:
      consolidated_dict = {}
      for (timestamp, value) in cached_datapoints:
          interval = timestamp - (timestamp % step)
          if interval in consolidated_dict:
              consolidated_dict[interval].append(value)
          else:
              consolidated_dict[interval] = [value]
      for interval in consolidated_dict:
          value = consolidate(func, consolidated_dict[interval])
          consolidated.append((interval, value))

  else:
      consolidated = cached_datapoints

  for (interval, value) in consolidated:
      try:
          i = int(interval - start) / step
          if i < 0:
              # cached data point is earlier then the requested data point.
              # meaning we can definitely ignore the cache result.
              # note that we cannot rely on the 'except'
              # in this case since 'values[-n]='
              # is equivalent to 'values[len(values) - n]='
              continue
          values[i] = value
      except:
          pass

  return values
