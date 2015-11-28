#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os        # For strerror
import mmap      # For setting up a shared memory region
import ctypes    # For doing the actual wrapping of librt & rwlock
import platform  # To figure which architecture we're running in
import tempfile  # To open a file to back our mmap

from ctypes.util import find_library

# Loads the library in which the functions we're wrapping are defined
librt = ctypes.CDLL(find_library('rt'), use_errno=True)

if platform.system() == 'Linux':
    if platform.architecture()[0] == '64bit':
        pthread_rwlock_t = ctypes.c_byte * 56
    elif platform.architecture()[0] == '32bit':
        pthread_rwlock_t = ctypes.c_byte * 32
    else:
        pthread_rwlock_t = ctypes.c_byte * 44
else:
    raise Exception("Unsupported operating system.")

pthread_rwlockattr_t = ctypes.c_byte * 8

PTHREAD_PROCESS_SHARED = 1

pthread_rwlockattr_t_p = ctypes.POINTER(pthread_rwlockattr_t)
pthread_rwlock_t_p = ctypes.POINTER(pthread_rwlock_t)

API = [
    ('pthread_rwlock_destroy', [pthread_rwlock_t_p]),
    ('pthread_rwlock_init', [pthread_rwlock_t_p, pthread_rwlockattr_t_p]),
    ('pthread_rwlock_unlock', [pthread_rwlock_t_p]),
    ('pthread_rwlock_wrlock', [pthread_rwlock_t_p]),
    ('pthread_rwlockattr_destroy', [pthread_rwlockattr_t_p]),
    ('pthread_rwlockattr_init', [pthread_rwlockattr_t_p]),
    ('pthread_rwlockattr_setpshared', [pthread_rwlockattr_t_p, ctypes.c_int]),
]


def error_check(result, func, arguments):
    name = func.__name__
    if result != 0:
        error = os.strerror(result)
        raise OSError(result, '{} failed {}'.format(name, error))


def augment_function(library, name, argtypes):
    function = getattr(library, name)
    function.argtypes = argtypes
    function.errcheck = error_check

# At the global level we add argument types and error checking to the
# functions:
for function, argtypes in API:
    augment_function(librt, function, argtypes)


class RWLock(object):
    def __init__(self):
        self.__setup(None)

    def __setup(self, _fd=None):
        try:
            # Define these guards so we know which attribution has failed
            buf, lock, lockattr, fd = None, None, None, None

            if _fd:
                # We're being called from __setstate__, all we have to do is
                # load the file descriptor of the backing file
                fd = _fd
            else:
                # Create a temporary file with an actual file descriptor, so
                # that child processes can receive the lock via apply from the
                # multiprocessing module
                fd, name = tempfile.mkstemp()
                os.write(fd, b'\0' * mmap.PAGESIZE)

            # mmap allocates page sized chunks, and the data structures we
            # use are smaller than a page. Therefore, we request a whole
            # page
            buf = mmap.mmap(fd, mmap.PAGESIZE, mmap.MAP_SHARED)
            if _fd:
                buf.seek(0)

            # Use the memory we just obtained from mmap and obtain pointers
            # to that data
            offset = ctypes.sizeof(pthread_rwlock_t)
            tmplock = pthread_rwlock_t.from_buffer(buf)
            lock_p = ctypes.byref(tmplock)
            tmplockattr = pthread_rwlockattr_t.from_buffer(buf, offset)
            lockattr_p = ctypes.byref(tmplockattr)

            if _fd is None:
                # Initialize the rwlock attributes and make it process shared
                librt.pthread_rwlockattr_init(lockattr_p)
                lockattr = tmplockattr
                librt.pthread_rwlockattr_setpshared(lockattr_p,
                                                    PTHREAD_PROCESS_SHARED)

                # Initialize the rwlock
                librt.pthread_rwlock_init(lock_p, lockattr_p)
                lock = tmplock
            else:
                # The data is already initialized in the mmap. We only have to
                # point to it
                lockattr = tmplockattr
                lock = tmplock

            # Finally initialize this instance's members
            self._fd = fd
            self._buf = buf
            self._lock = lock
            self._lock_p = lock_p
            self._lockattr = lockattr
            self._lockattr_p = lockattr_p
        except:
            if lock:
                try:
                    librt.pthread_rwlock_destroy(lock_p)
                    lock_p, lock = None, None
                except:
                    # We really need this reference gone to free the buffer
                    lock_p, lock = None, None
            if lockattr:
                try:
                    librt.pthread_rwlockattr_destroy(lockattr_p)
                    lockattr_p, lockattr = None, None
                except:
                    # We really need this reference gone to free the buffer
                    lockattr_p, lockattr = None, None
            if buf:
                try:
                    buf.close()
                except:
                    pass
            if fd:
                try:
                    os.close(fd)
                except:
                    pass
            raise

    def acquire_read(self):
        librt.pthread_rwlock_rdlock(self._lock_p)

    def acquire_write(self):
        librt.pthread_rwlock_wrlock(self._lock_p)

    def release(self):
        librt.pthread_rwlock_unlock(self._lock_p)

    def __getstate__(self):
        # We only care about the file descriptor of the memory-mapped file.
        # Everything else can be recalculated later.
        return {'_fd': self._fd}

    def __setstate__(self, state):
        self.__setup(state['_fd'])

    def _del_lockattr(self):
        librt.pthread_rwlockattr_destroy(self._lockattr_p)
        self._lockattr, self._lockattr_p = None, None

    def _del_lock(self):
        librt.pthread_rwlock_destroy(self._lock_p)
        self._lock, self._lock_p = None, None

    def _del_buf(self):
        self._buf.close()
        self._buf = None

    def __del__(self):
        for name in '_lockattr _lock _buf'.split():
            attr = getattr(self, name, None)
            if attr is not None:
                func = getattr(self, '_del{}'.format(name))
                func()
        try:
            os.close(self._fd)
        except OSError:
            # Nothing we can do. We opened the file descriptor, we have to
            # close it. If we can't, all bets are off.
            pass