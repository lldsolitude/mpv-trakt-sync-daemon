#!/usr/bin/env python3
import json
import threading
import time

import guessit
import os
import requests

import mpv

TRAKT_CLIENT_ID = '24c7a86d0a55334a9575734decac760cea679877fcb60b0983cbe45996242dd7'
TRAKT_ID_CACHE_JSON = 'trakt_ids.json'

MPV_WINDOWS_NAMED_PIPE_PATH = r'\\.\pipe\mpv'
MPV_POSIX_SOCKET_PATH = '/tmp/mpv-socket'

SECONDS_BETWEEN_MPV_RUNNING_CHECKS = 5.0
SECONDS_BETWEEN_MPV_EVENT_AND_TRAKT_SYNC = 2.0
SECONDS_BETWEEN_REGULAR_GET_PROPERTY_COMMANDS = 10.0
FACTOR_MUST_WATCH_BEFORE_SCROBBLE = 0.1
PERCENT_MINIMAL_PLAYBACK_POSITION_BEFORE_SCROBBLE = 90.0

monitored_directories = ['/mnt/sybefra/', '/run/media/hans/wde/',
                         'http://syncthing-hub-becker-frankfurt-odroid:1113/']

last_is_paused = None
last_playback_position = None
last_path = None
last_duration = None
last_file_start_timestamp = None

is_local_state_dirty = True

next_sync_timer = None
next_regular_timer = None


def on_command_response(monitor, command, response):
    global last_is_paused, last_playback_position, last_path, last_duration, last_file_start_timestamp
    global next_sync_timer, next_regular_timer
    global is_local_state_dirty

    last_command_elements = command['command']
    if last_command_elements[0] == 'get_property':
        if last_command_elements[1] == 'pause':
            last_is_paused = response['data']
            if not last_is_paused and last_file_start_timestamp is None:
                last_file_start_timestamp = time.time()
        elif last_command_elements[1] == 'percent-pos':
            last_playback_position = response['data']
        elif last_command_elements[1] == 'path':
            last_path = response['data']
        elif last_command_elements[1] == 'duration':
            last_duration = response['data']
        if last_is_paused is not None \
                and last_playback_position is not None \
                and last_path is not None \
                and last_duration is not None:
            if is_local_state_dirty:
                if next_sync_timer is not None:
                    next_sync_timer.cancel()
                next_sync_timer = threading.Timer(SECONDS_BETWEEN_MPV_EVENT_AND_TRAKT_SYNC, sync_to_trakt,
                                                  (last_is_paused, last_playback_position, last_path, last_duration,
                                                   last_file_start_timestamp))
                next_sync_timer.start()

            last_is_paused = None
            last_playback_position = None
            last_path = None
            last_duration = None


def on_event(monitor, event):
    event_name = event['event']

    # when a new file starts, acts as a new mpv instance got connected
    if event_name == 'start-file':
        on_disconnected()
        on_connected(monitor)

    if event_name == 'pause' or event_name == 'unpause' or event_name == 'seek':
        global is_local_state_dirty
        is_local_state_dirty = True
        issue_scrobble_commands(monitor)


def on_connected(monitor):
    issue_scrobble_commands(monitor)


def on_disconnected():
    global last_file_start_timestamp
    last_file_start_timestamp = None

    global is_local_state_dirty
    is_local_state_dirty = True

    if next_sync_timer is not None:
        next_sync_timer.cancel()

    if next_regular_timer is not None:
        next_regular_timer.cancel()


def issue_scrobble_commands(monitor):
    monitor.send_get_property_command('path')
    monitor.send_get_property_command('percent-pos')
    monitor.send_get_property_command('pause')
    monitor.send_get_property_command('duration')
    schedule_regular_timer(monitor)


def schedule_regular_timer(monitor):
    global next_regular_timer
    if next_regular_timer is not None:
        next_regular_timer.cancel()
    next_regular_timer = threading.Timer(SECONDS_BETWEEN_REGULAR_GET_PROPERTY_COMMANDS, issue_scrobble_commands,
                                         [monitor])
    next_regular_timer.start()


def get_watch_state(is_paused, playback_position, duration, start_time):
    if start_time is not None:
        watch_time = time.time() - start_time
        # only consider a session finished if
        #   at least a minimal playback position is reached
        # and
        #   the session is running long enough
        if playback_position >= PERCENT_MINIMAL_PLAYBACK_POSITION_BEFORE_SCROBBLE \
                and watch_time >= duration * FACTOR_MUST_WATCH_BEFORE_SCROBBLE:
            return 'finished'
    if is_paused:
        return 'paused'
    else:
        return 'watching'


def sync_to_trakt(is_paused, playback_position, path, duration, start_time):
    print('sync_to_trakt', get_watch_state(is_paused, playback_position, duration, start_time))

    global is_local_state_dirty

    for monitored_directory in monitored_directories:
        if path.startswith(monitored_directory):
            guess = guessit.guessit(path)

            # load cached ids
            if os.path.exists(TRAKT_ID_CACHE_JSON):
                with open(TRAKT_ID_CACHE_JSON) as file:
                    id_cache = json.load(file)
            else:
                id_cache = {
                    'movies': {},
                    'shows': {}
                }

            if guess['type'] == 'episode':
                if guess['title'].lower() not in id_cache['shows']:
                    print('requesting trakt id for show', guess['title'])
                    req = requests.get('https://api.trakt.tv/search/show?field=title&query=%s' % guess['title'],
                                       headers={'trakt-api-version': '2', 'trakt-api-key': TRAKT_CLIENT_ID})
                    if req.status_code == 200 and len(req.json()) > 0:
                        id_cache['shows'][guess['title'].lower()] = req.json()[0]['show']['ids']['trakt']
                    else:
                        print('trakt request failed or unknown show', guess)
                print('scrobbling show', id_cache['shows'][guess['title'].lower()])
                is_local_state_dirty = False
            elif guess['type'] == 'movie':
                print('requesting trakt id for movie', guess['title'])
                if guess['title'].lower() not in id_cache['movies']:
                    req = requests.get('https://api.trakt.tv/search/movie?field=title&query=%s' % guess['title'],
                                       headers={'trakt-api-version': '2', 'trakt-api-key': TRAKT_CLIENT_ID})
                    if req.status_code == 200 and len(req.json()) > 0:
                        id_cache['movies'][guess['title'].lower()] = req.json()[0]['movie']['ids']['trakt']
                    else:
                        print('trakt request failed or unknown movie', guess)
                print('scrobbling movie', id_cache['movies'][guess['title'].lower()])
                is_local_state_dirty = False
            else:
                print('Unknown guessit type', guess['type'])

            # update cached ids file
            with open(TRAKT_ID_CACHE_JSON, mode='w') as file:
                json.dump(id_cache, file)

            return


def main():
    monitor = mpv.MpvMonitor.create(MPV_POSIX_SOCKET_PATH, MPV_WINDOWS_NAMED_PIPE_PATH,
                                    on_connected, on_event, on_command_response, on_disconnected)
    while True:
        if monitor.can_open():
            monitor.run()
            print('mpv closed')
            # If run() returns, mpv was closed.
            # If we try to instantly check for via can_open() and open it again, mpv crashes (at least on Windows).
            # So we need to give mpv some time to close gracefully.
            time.sleep(1)
        else:
            # sleep before next attempt
            try:
                print('mpv not open sleeping')
                time.sleep(SECONDS_BETWEEN_MPV_RUNNING_CHECKS)
            except KeyboardInterrupt:
                print('terminating')
                quit(0)


if __name__ == '__main__':
    main()
