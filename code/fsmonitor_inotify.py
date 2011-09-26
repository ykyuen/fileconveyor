"""fsmonitor_inotify.py FSMonitor subclass for inotify on Linux kernel >= 2.6.13"""


__author__ = "Wim Leers (work@wimleers.com)"
__version__ = "$Rev$"
__date__ = "$Date$"
__license__ = "GPL"


from fsmonitor import *
import pyinotify
from pyinotify import WatchManager, \
                      ThreadedNotifier, \
                      ProcessEvent, \
                      WatchManagerError
import time
import os
import stat
import sys



# Define exceptions.
class FSMonitorInotifyError(FSMonitorError): pass


class FSMonitorInotify(FSMonitor):
    """inotify support for FSMonitor"""


    EVENTMAPPING = {
        FSMonitor.CREATED             : pyinotify.IN_CREATE,
        FSMonitor.MODIFIED            : pyinotify.IN_MODIFY | pyinotify.IN_ATTRIB,
        FSMonitor.DELETED             : pyinotify.IN_DELETE,
        FSMonitor.MONITORED_DIR_MOVED : pyinotify.IN_MOVE_SELF,
        FSMonitor.DROPPED_EVENTS      : pyinotify.IN_Q_OVERFLOW,
    }


    def __init__(self, callback, persistent=False, trigger_events_for_initial_scan=False, ignored_dirs=[], dbfile="fsmonitor.db", parent_logger=None):
        FSMonitor.__init__(self, callback, persistent, trigger_events_for_initial_scan, ignored_dirs, dbfile, parent_logger)
        self.logger.info("FSMonitor class used: FSMonitorInotify.")
        self.wm             = None
        self.notifier       = None
        self.pathscanner_files_created  = []
        self.pathscanner_files_modified = []
        self.pathscanner_files_deleted  = []


    def __fsmonitor_event_to_inotify_event(self, event_mask):
        """map an FSMonitor event to an inotify event"""
        inotify_event_mask = 0
        for fsmonitor_event_mask in self.__class__.EVENTMAPPING.keys():
            if event_mask & fsmonitor_event_mask:
                inotify_event_mask = inotify_event_mask | self.__class__.EVENTMAPPING[fsmonitor_event_mask]
        return inotify_event_mask


    def inotify_path_to_monitored_path(self, path):
        """map a pathname (as received in an inotify event) to its
        corresponding monitored path
        """
        for monitored_path in self.monitored_paths.keys():
            if os.path.commonprefix([path, monitored_path]) == monitored_path:
                return monitored_path


    def __add_dir(self, path, event_mask):
        """override of FSMonitor.__add_dir()"""

        # Immediately start monitoring this directory.
        event_mask_inotify = self.__fsmonitor_event_to_inotify_event(event_mask)
        try:
            wdd = self.wm.add_watch(path.encode('utf-8'), event_mask_inotify, proc_fun=self.process_event, rec=True, auto_add=True, quiet=False)
        except WatchManagerError, e:
            raise FSMonitorError, "Could not monitor '%s', reason: %s" % (path, e)
        # Verify that inotify is able to monitor this directory and all of its
        # subdirectories.
        for monitored_path in wdd:
            if wdd[monitored_path] < 0:
                code = wdd[monitored_path]
                raise FSMonitorError, "Could not monitor %s (%d)" % (monitored_path, code)
        self.monitored_paths[path] = MonitoredPath(path, event_mask, wdd)
        self.monitored_paths[path].monitoring = True

        if self.persistent:
            # Generate the missed events. This implies that events that
            # occurred while File Conveyor was offline (or not yet in use)
            # will *always* be generated, whether this is the first run or the
            # thousandth.
            FSMonitor.generate_missed_events(self, path)
        else:
            # Perform an initial scan of the directory structure. If this has
            # already been done, then it will return immediately.
            self.pathscanner.initial_scan(path)

        return self.monitored_paths[path]


    def __remove_dir(self, path):
        """override of FSMonitor.__remove_dir()"""
        if path in self.monitored_paths.keys():
            self.wm.rm_watch(path, rec=True, quiet=True)
            del self.monitored_paths[path]


    def run(self):
        # Setup. Ensure that this isn't interleaved with any other thread, so
        # that the DB setup continues as expected.
        self.lock.acquire()
        FSMonitor.setup(self)
        self.process_event = FSMonitorInotifyProcessEvent(self)
        self.lock.release()

        # Set up inotify.
        self.wm = WatchManager()
        self.notifier = ThreadedNotifier(self.wm, self.process_event)

        self.notifier.start()

        while not self.die:
            self.__process_queues()
            time.sleep(0.5)

        self.notifier.stop()


    def stop(self):
        """override of FSMonitor.stop()"""

        # Let the thread know it should die.
        self.lock.acquire()
        self.die = True
        self.lock.release()

        # Stop monitoring each monitored path.
        for path in self.monitored_paths.keys():
            self.__remove_dir(path)


    def __process_pathscanner_updates(self, update_list, callback):
        self.lock.acquire()
        if len(update_list) > 0:
            callback(update_list)
            del update_list[:] # Clear the list with updates.
        self.lock.release()


    def __process_queues(self):
        # Process "add monitored path" queue.
        self.lock.acquire()
        if not self.add_queue.empty():
            (path, event_mask) = self.add_queue.get()
            self.lock.release()
            self.__add_dir(path, event_mask)
        else:
            self.lock.release()

        # Process "remove monitored path" queue.
        self.lock.acquire()
        if not self.remove_queue.empty():
            path = self.add_queue.get()
            self.lock.release()
            self.__remove_dir(path)
        else:
            self.lock.release()

        # These calls to PathScanner is what ensures that FSMonitor.db
        # remains up-to-date. (These lists of files to add, update and delete
        # from the DB are applied to PathScanner.)
        self.__process_pathscanner_updates(self.pathscanner_files_created,  self.pathscanner.add_files   )
        self.__process_pathscanner_updates(self.pathscanner_files_modified, self.pathscanner.update_files)
        self.__process_pathscanner_updates(self.pathscanner_files_deleted,  self.pathscanner.delete_files)




class FSMonitorInotifyProcessEvent(ProcessEvent):


    # On Linux, you can choose which encoding is used for your file system's
    # file names. Hence, we better detect the file system's encoding so we
    # know what to decode from in __ensure_unicode(). 
    encoding = sys.getfilesystemencoding()


    def __init__(self, fsmonitor):
        ProcessEvent.__init__(self)
        self.fsmonitor_ref      = fsmonitor
        self.discovered_through = "inotify"


    def __update_pathscanner_db(self, pathname, event_type):
        """use PathScanner.(add|update|delete)_files() to queue updates for
        PathScanner's DB
        """
        (path, filename) = os.path.split(pathname)
        if event_type == FSMonitor.DELETED:
            # Build tuple for deletion of row in PathScanner's DB.
            t = (path, filename)
            self.fsmonitor_ref.pathscanner_files_deleted.append(t)
        else:
            # Build tuple for PathScanner's DB of the form (path, filename,
            # mtime), with mtime = -1 when it's a directory.
            st = os.stat(pathname)
            is_dir = stat.S_ISDIR(st.st_mode)
            if not is_dir:
                mtime = st[stat.ST_MTIME]
                t = (path, filename, mtime)
            else:
                t = (path, filename, -1)

            # Update PathScanner's DB.
            if event_type == FSMonitor.CREATED:
                self.fsmonitor_ref.pathscanner_files_created.append(t)
            else:
                self.fsmonitor_ref.pathscanner_files_modified.append(t)


    @classmethod
    def __ensure_unicode(cls, event):
        event.path = event.path.decode(cls.encoding)
        event.pathname = event.pathname.decode(cls.encoding)
        return event


    def process_IN_CREATE(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_CREATE event has occurred for '%s'." % (event.pathname))
        self.__update_pathscanner_db(event.pathname, FSMonitor.CREATED)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.CREATED, self.discovered_through)


    def process_IN_DELETE(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_DELETE event has occurred for '%s'." % (event.pathname))
        self.__update_pathscanner_db(event.pathname, FSMonitor.DELETED)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.DELETED, self.discovered_through)


    def process_IN_MODIFY(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_MODIFY event has occurred for '%s'." % (event.pathname))
        self.__update_pathscanner_db(event.pathname, FSMonitor.MODIFIED)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.MODIFIED, self.discovered_through)


    def process_IN_ATTRIB(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_ATTRIB event has occurred for '%s'." % (event.pathname))
        self.__update_pathscanner_db(event.pathname, FSMonitor.MODIFIED)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.MODIFIED, self.discovered_through)


    def process_IN_MOVE_SELF(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_MOVE_SELF event has occurred for '%s'." % (event.pathname))
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.MONITORED_DIR_MOVED, self.discovered_through)


    def process_IN_Q_OVERFLOW(self, event):
        event = self.__ensure_unicode(event)
        if FSMonitor.is_in_ignored_directory(self.fsmonitor_ref, event.path):
            return
        self.fsmonitor_ref.logger.debug("inotify reports that an IN_Q_OVERFLOW event has occurred for '%s'." % (event.pathname))
        monitored_path = self.fsmonitor_ref.inotify_path_to_monitored_path(event.path)
        FSMonitor.trigger_event(self.fsmonitor_ref, monitored_path, event.pathname, FSMonitor.DROPPED_EVENTS, self.discovered_through)


    def process_default(self, event):
        # Event not supported!
        self.fsmonitor_ref.logger.debug("inotify reports that an unsupported event (mask: %d, %s) has occurred for '%s'." % (event.mask, event.maskname, event.pathname))
        pass
