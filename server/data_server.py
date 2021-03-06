#!/usr/bin/env python3
"""DataServer is a nasty bit of code, there's no two ways about it.

The goal is to serve logger data, e.g. for widgets and displays. The
class is initialized with a websocket.

Calling get_fields() listens on the websocket for a JSON string
encoding a list of pairs:

  [(field_1_name, field_1_seconds), (field_2_name, field_2_seconds), ...]

where field_X_seconds is a float representing the number of seconds of
back data being requested.

This field list is passed to serve_fields(), which retrieves back data
(in this implementation using a DatabaseReader), then checks back
every <interval> seconds for more data to send along.

This file includes a main() routine for testing, but in practice I
expect the class to primarily be called from the LoggerManager.
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
import websockets

sys.path.append('.')

from logger.utils.read_json import read_json, parse_json

from logger.readers.database_reader import DatabaseReader
from database.settings import DEFAULT_DATABASE, DEFAULT_DATABASE_HOST
from database.settings import DEFAULT_DATABASE_USER
from database.settings import DEFAULT_DATABASE_PASSWORD

# Number of times we'll try a failing logger before giving up
DEFAULT_MAX_TRIES = 3
EPSILON = 0.00001

# To keep logger/config names unique, we'll prepend cruise_id,
# separating them by CRUISE_ID_SEPARATOR; e.g. NBP1700:knud
CRUISE_ID_SEPARATOR = ':'

LOGGING_FORMAT = '%(asctime)-15s %(filename)s:%(lineno)d %(message)s'
LOG_LEVELS = {0:logging.WARNING, 1:logging.INFO, 2:logging.DEBUG}

################################################################################
class DataServer:
  ############################
  def __init__(self, websocket, field_list=None, interval=1,
               host=DEFAULT_DATABASE_HOST, user=DEFAULT_DATABASE_USER,
               password=DEFAULT_DATABASE_PASSWORD, database=DEFAULT_DATABASE):
    self.websocket = websocket
    # Which fields to serve to client
    self.interval = interval
    self.field_list = field_list
    self.field_list_lock = threading.Lock()

    self.host = host
    self.user = user
    self.password = password
    self.database = database

    self.quit_flag = False

  ############################
  @asyncio.coroutine
  async def serve_data(self):
    """Start serving on websocket. Assumes we've got our own event loop."""

    field_list = await self.get_field_list()
    await self.serve_fields(field_list)

  ############################
  def quit(self):
    """Exit the loop and shut down all loggers."""
    self.quit_flag = True  

  ############################
  @asyncio.coroutine
  async def get_field_list(self):
    """Get the fields we're interested in having served.
    """
    message = await self.websocket.recv()
    logging.info('Received data request: "%s"', message)
    if not message:
      logging.info('Received empty data request, doing nothing.')
      return

    try:
      self.field_list = json.loads(message)
      return self.field_list
    except json.JSONDecodeError:
      logging.info('get_fields(): unparseable JSON request: "%s"', message)
    
  ############################
  @asyncio.coroutine
  async def serve_fields(self, field_list):
    """Serve data, if it exists, from database, if it exists, using default
    database location, tables, user and password.

    NOTE: This is the kind of code your mother warned you about. It's a
    quick first pass, and will therefore follow me to my grave and haunt
    you for years to come. For the love of Guido, please clean this up.
    """
    for (field_name, num_secs) in field_list:
      logging.info('Requesting field: %s, %g secs.', field_name, num_secs)
      
    # Get requested back data. Note that we may have had different
    # back data time spans for different fields. Because some of these
    # might be extremely voluminous (think 30 minutes of winch data),
    # take the computational hit of initially creating a separate
    # reader for each backlog.
    fields = []
    back_data = {}
    for (field_name, num_secs) in field_list:
      fields.append(field_name)
      if not num_secs in back_data:
        back_data[num_secs] = []
      back_data[num_secs].append(field_name)

    results = {}
    now = time.time()
    for (num_secs, field_list) in back_data.items():
      # Create a DatabaseReader to get num_secs worth of back data for
      # these fields. Provide a start_time of num_secs ago, and no
      # stop_time, so we get everything up to present.
      logging.debug('Creating DatabaseReader for %s', field_list)
      logging.debug('Requesting %g seconds of timestamps from %f-%f',
                      num_secs, now-num_secs, now)
      reader = DatabaseReader(fields, self.database, self.host,
                              self.user, self.password)
      num_sec_results = reader.read_time_range(start_time=now-num_secs)
      logging.debug('results: %s', num_sec_results)
      results.update(num_sec_results)
                     
    # Now that we've gotten all the back results, create a single
    # DatabaseReader to read all the fields.
    reader = DatabaseReader(fields, self.database, self.host,
                            self.user, self.password)
    max_timestamp_seen = 0
    
    while not self.quit_flag:
      # If we do have results, package them up and send them
      if results:
        send_message = json.dumps(results)
        logging.debug('Data server sending: %s', send_message)
        try:
          await self.websocket.send(send_message)
        except websockets.exceptions.ConnectionClosed:
          return

      # New results or not, take a nap before trying to fetch more results
      logging.debug('Sleeping %g seconds', self.interval)
      await asyncio.sleep(self.interval)

      # What's the timestamp of the most recent result we've seen?
      # Each value should be a list of (timestamp, value) pairs. Look
      # at the last timestamp in each value list.
      for field in results:
        last_timestamp = results[field][-1][0]
        max_timestamp_seen = max(max_timestamp_seen, last_timestamp)

      # Bug's corner case: if we didn't retrieve any data on the first
      # time through (because it was all too old), max_timestamp_seen
      # will be zero, causing us to retrieve *all* the data in the DB
      # on the next iteration. If we do find that max_timestamp_seen
      # is zero, set it to "now" to prevent this.
      if not max_timestamp_seen:
        max_timestamp_seen = now

      logging.debug('Results: %s', results)
      if len(results):
        logging.info('Received %d fields, max timestamp %f',
                     len(results), max_timestamp_seen)

      # Check whether there are results newer than latest timestamp
      # we've already seen.
      results = reader.read_time_range(start_time=max_timestamp_seen + EPSILON)


 ############################
@asyncio.coroutine
async def serve_websocket_data(websocket, path):
  logging.warning('serve_websocket_data: %s', websocket)
  data_server = DataServer(websocket)
  await data_server.serve_data()

################################################################################
################################################################################
if __name__ == '__main__':
  import argparse
  
  parser = argparse.ArgumentParser()

  # Optional address for websocket server from which we'll accept
  # connections from LoggerRunners willing to accept dispatched
  # configs.
  parser.add_argument('--websocket', dest='websocket', action='store',
                      help='Host:port on which to serve data')
  parser.add_argument('--host', dest='host', action='store',
                      default=DEFAULT_DATABASE_HOST,
                      help='Database host name')
  parser.add_argument('--user', dest='user', action='store',
                      default=DEFAULT_DATABASE_USER,
                      help='Database user name')
  parser.add_argument('--database', dest='database', action='store',
                      default=DEFAULT_DATABASE,
                      help='Database database name')
  parser.add_argument('--password', dest='password', action='store',
                      default=DEFAULT_DATABASE_PASSWORD,
                      help='Database password name')

  parser.add_argument('--interval', dest='interval', action='store',
                      type=float, default=1,
                      help='How many seconds to sleep between logger checks.')

  parser.add_argument('-v', '--verbosity', dest='verbosity', default=0,
                      action='count', help='Increase output verbosity')
  args = parser.parse_args()

  # Set logging verbosity
  args.verbosity = min(args.verbosity, max(LOG_LEVELS))
  logging.getLogger().setLevel(LOG_LEVELS[args.verbosity])

  try:
    host, port = args.websocket.split(':')
    port = int(port)
  except ValueError:
    logging.error('--websocket argument must be host:port')
    sys.exit(1)
    
  try:
    event_loop = asyncio.get_event_loop()
    websocket_server = websockets.serve(serve_websocket_data, host, port)
    event_loop.run_until_complete(websocket_server)
    event_loop.run_forever()
  except OSError:
    logging.warning('Failed to open websocket %s:%s', host, port)

  
  
