#!/usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function

import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--pk', action='store_true', help='Use policykit.')
parser.add_argument('--debug',
	action='store_true', help='Verbose operation mode.')
optz = parser.parse_args()


import itertools as it, operator as op, functools as ft
from io import open
from contextlib import contextmanager, closing
from collections import namedtuple, deque
from subprocess import Popen, PIPE, STDOUT
import os, sys, errno, struct, ctypes, logging

logging.basicConfig( level=logging.DEBUG\
	if optz.debug else logging.WARNING )
log = logging.getLogger()


class timespec(ctypes.Structure):
	_fields_ = [
		('tv_sec', ctypes.c_long),
		('tv_nsec', ctypes.c_long) ]

librt = ctypes.CDLL('librt.so.1', use_errno=True)
clock_gettime = librt.clock_gettime
clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(timespec)]

def monotonic_time():
	t = timespec()
	if clock_gettime(1, ctypes.pointer(t)) != 0: # <linux/time.h>
		errno_ = ctypes.get_errno()
		raise OSError(errno_, os.strerror(errno_))
	return t.tv_sec + t.tv_nsec * 1e-9


class Inotify(object):

	def get_flags():
		import re
		re_define = re.compile('^#define\s+(IN_\w+)\s+([^/]+)')
		for prefix in '', 'usr', 'usr/local':
			try: header = open('/{}/include/linux/inotify.h'.format(prefix))
			except (OSError, IOError): continue
			flags = dict()
			consts = dict( (k, getattr(os, k))
				for k in dir(os) if isinstance(getattr(os, k), int) )
			for line in iter(header.readline, ''):
				match = re.search('^#define\s+(IN_\w+)\s+([^/]+)', line)
				if not match: continue
				k, line = match.groups()
				v = ''
				for line in it.chain([line], iter(header.readline, '')):
					if '/' in line: line = line.split('/', 1)[0]
					line = line.strip()
					v += line
					if not line.endswith('\\'): break
					v = v.rstrip('\\').rstrip()
				try: flags[k] = eval(v, consts, flags)
				except NameError as err: pass
			return dict((k[3:], v) for k,v in flags.viewitems()) # drop IN_ prefix
		else: raise OSError('Unable to read inotify.h header')

	class flags(object): pass

	for k,v in get_flags().viewitems(): setattr(flags, k, v)
	get_flags = staticmethod(get_flags)
	del k,v

	def _libc_call(self, name, *args):
		ret = getattr(self.libc, name)(*args)
		if ret == -1:
			errno_ = getattr(self.libc, '__errno_location').contents.value
			raise OSError( errno_, 'libc call '
				'"{}{}" error: {}'.format(name, args, os.strerror(errno_)) )
		return ret


	@classmethod
	@contextmanager
	def watch(cls, path, flags):
		watcher = cls()
		watcher.add_watch(path, flags)
		yield watcher
		watcher.close()

	def __enter__(self): pass
	def __exit__(self, exc_type, exc_val, exc_tb): self.close()


	def __init__(self):
		self.libc = ctypes.cdll.LoadLibrary('libc.so.6')
		getattr(self.libc, '__errno_location').restype = ctypes.POINTER(ctypes.c_int)
		self.fd, self.closed = self._libc_call('inotify_init'), False
		self.wd_map = dict()

	def add_watch(self, path, flags):
		wd = self._libc_call( 'inotify_add_watch', self.fd,
			path.encode(sys.getfilesystemencoding())
				if isinstance(path, unicode) else path, flags )
		self.wd_map[wd] = path

	def remove_watch(self, wd):
		return self._libc_call('inotify_rm_watch', self.fd, wd)

	def poll(self, timeout=None):
		from select import select
		r,w,x = select([self.fd], [], [self.fd], timeout)
		return list() if self.fd not in r\
			else list(self.process_events())
	next = poll

	event = namedtuple('Event', 'path mask cookie name')

	def process_events(self):
		bs, bsm = 8192, 1
		while bsm < 100:
			try: events = os.read(self.fd, bs*bsm)
			except OSError as err:
				if err.errno == errno.EINVAL: bsm += 1
				else: raise
			else: break
		else: raise RuntimeError

		bb, bs = 0, struct.calcsize(b'iIII')
		while True:
			be = bb + bs
			event = events[bb:be]
			if len(event) == 0: break
			wd, mask, cookie, name_len = struct.unpack(b'iIII', event)
			bb = be + name_len
			name = struct.unpack(
				'{}s'.format(name_len).encode('ascii'),
				events[be:bb] )[0].rstrip(b'\0')
			yield self.event(self.wd_map[wd], mask, cookie, name)

	def close(self):
		if not self.closed: os.close(self.fd)
	__del__ = close


class CancelRequest(Exception): pass
class SkipRequest(Exception): pass

def get_pass():
	cache_path = '/run/initramfs/.password.cache'
	try: return open(cache_path, 'rb').read().strip()
	except (OSError, IOError): raise SkipRequest


class PKExecError(Exception): pass
class SocketSendError(Exception): pass

def send_pass(sock, password):
	if optz.pk:
		proc = Popen( [ 'pkexec', '/lib/systemd/systemd-reply-password',
				'1' if password is not None else '0', sock ],
			stdin=PIPE, stdout=PIPE, stderr=STDOUT )
		if password is not None:
			proc.stdin.write(password)
			proc.stdin.close()
		proc_debug = proc.stdout.read()
		exit_code = proc.wait()
		if exit_code: raise PKExecError(exit_code, proc_debug)

	else:
		import socket
		try:
			with closing(socket.socket(
					socket.AF_UNIX, socket.SOCK_DGRAM )) as s:
				s.connect(sock)
				s.send(b'+' + password)
		except socket.error as err: raise SocketSendError(err)


def request_poll(path):
	from ConfigParser import SafeConfigParser

	if isinstance(path, unicode): path = path.encode(sys.getfilesystemencoding())
	req_flags = Inotify.flags.CLOSE_WRITE | Inotify.flags.MOVED_TO
	with Inotify.watch(path, req_flags | Inotify.flags.DELETE) as watcher:
		# Generate synthetic events for all paths that are already there
		events = deque(Inotify.event( path,
			req_flags, 0, name ) for name in os.listdir(path))
		events_processed = set() # these are kept until DELETE
		msgs_processed = set() # to skip repeated queries
		while True:
			if not events: events.extend(watcher.poll(10))
			while events:
				# Check whether event should be processed
				ev = events.popleft()
				if not ev.name.startswith(b'ask.'): continue
				elif not ev.mask & req_flags: # handle misc flags here
					if ev.mask & Inotify.flags.DELETE:
						log.debug('Detected processed req-file removal: {!r}'.format(ev.name))
						events_processed.discard(ev.name)
					continue
				elif ev.name in events_processed:
					log.debug( 'Skipping event for'
						' already processed req-file: {!r}'.format(ev.name) )
					continue

				log.debug('Processing req-file: {!r}'.format(ev.name))
				events_processed.add(ev.name)

				# Read/check configuration, prepare response, if any
				cfg = SafeConfigParser()
				if not cfg.read(os.path.join(ev.path, ev.name)):
					log.debug('Failed to read/parse req-file, skipping')
					continue
				msg = cfg.get('Ask', 'Message')
				if msg and msg in msgs_processed:
					log.debug( 'Repeated request with'
						' the same message ({!r}), skipping'.format(msg) )
					continue
				pid = cfg.getint('Ask', 'PID')
				try: os.kill(pid, 0)
				except OSError as err:
					if err.errno == errno.ESRCH:
						log.debug('Requesting PID seem to be dead, skipping request')
						continue
					raise
				ts_chk = cfg.getint('Ask', 'NotAfter')
				sock = cfg.get('Ask', 'Socket')
				try: password = get_pass()
				except SkipRequest:
					log.debug('Got signal to skip request')
					continue
				except CancelRequest:
					log.debug('Got signal to cancel request')
					password = None

				# Check whether request is still valid, send response
				if not ts_chk or monotonic_time() < ts_chk*1e-6:
					xev_delete, xevs = False, list(events)
					events.clear()
					for xev in it.chain(xevs, watcher.poll(0)): # check if req-file was removed
						if not xev_delete and xev.name == ev.name: # drop all other events for path
							if xev.mask & Inotify.flags.DELETE: # ...unless they come after DELETE
								log.debug('Detected request file removal, skipping')
								xev_delete = True
						else: events.append(xev)
					if not xev_delete:
						try: send_pass(sock, password)
						except PKExecError as err:
							exit_code, proc_debug = err.args
							log.warn( b'Failed to authorize (PolicyKit) / send reply (exit code: '
								+ unicode(exit_code).encode('ascii') + b'), debug info:\n' + proc_debug )
							continue
						except SocketSendError as err:
							log.warn('Error sending password: {}'.format(err.args[0]))
							continue
						else:
							if msg:
								if len(msgs_processed) > 50: # simple overflow protection
									msgs_processed = set(list(msgs_processed)[:20])
								msgs_processed.add(msg)
							log.debug('Successfully processed request: {!r}'.format(ev.name))
				else: log.debug('Request has expired, skipping')


try: request_poll('/run/systemd/ask-password')
except KeyboardInterrupt: pass
