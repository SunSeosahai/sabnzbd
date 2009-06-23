#!/usr/bin/python -OO
# Copyright 2008-2009 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import sys
if sys.version_info < (2,4):
    print "Sorry, requires Python 2.4 or higher."
    sys.exit(1)

import logging
import logging.handlers
import os
import getopt
import signal
import glob
import socket
import platform
import time

try:
    import Cheetah
    if Cheetah.Version[0] != '2':
        raise ValueError
except:
    print "Sorry, requires Python module Cheetah 2.0rc7 or higher."
    sys.exit(1)

import cherrypy
if not cherrypy.__version__.startswith("3.2"):
    print "Sorry, requires Python module Cherrypy 3.2 (use the included version)"
    sys.exit(1)

from cherrypy import _cpserver
from cherrypy import _cpwsgi_server

try:
    from sqlite3 import version as sqlite3_version
except:
    try:
        from pysqlite2.dbapi2 import version as sqlite3_version
    except:
        print "Sorry, requires Python module sqlite3 (pysqlite2 in python2.4)"
        if os.name != 'nt':
            print "Try: apt-get install python-pysqlite2"
        sys.exit(1)

import sabnzbd
import sabnzbd.interface
from sabnzbd.constants import *
import sabnzbd.newsunpack
from sabnzbd.misc import get_user_shellfolders, launch_a_browser, real_path, \
     check_latest_version, panic_tmpl, panic_port, panic_fwall, panic, exit_sab, \
     panic_xport, notify, split_host, convert_version, get_ext, create_https_certificates
import sabnzbd.scheduler as scheduler
import sabnzbd.config as config
import sabnzbd.cfg
import sabnzbd.downloader as downloader
from sabnzbd.lang import T
from sabnzbd.utils import osx

from threading import Thread

LOG_FLAG = False  # Global for this module, signalling loglevel change


#------------------------------------------------------------------------------
signal.signal(signal.SIGINT, sabnzbd.sig_handler)
signal.signal(signal.SIGTERM, sabnzbd.sig_handler)

try:
    import win32api
    win32api.SetConsoleCtrlHandler(sabnzbd.sig_handler, True)
except ImportError:
    if sabnzbd.WIN32:
        print "Sorry, requires Python module PyWin32."
        sys.exit(1)


def guard_loglevel():
    """ Callback function for guarding loglevel """
    global LOG_FLAG
    LOG_FLAG = True


#------------------------------------------------------------------------------
class FilterCP3:
    ### Filter out all CherryPy3-Access logging that we receive,
    ### because we have the root logger
    def __init__(self):
        pass
    def filter(self, record):
        _cplogging = record.module == '_cplogging'
        # Python2.4 fix
        # record has no attribute called funcName under python 2.4
        if hasattr(record, 'funcName'):
            access = record.funcName == 'access'
        else:
            access = True
        return not (_cplogging and access)


class guiHandler(logging.Handler):
    """
    Logging handler collects the last warnings/errors/exceptions
    to be displayed in the web-gui
    """
    def __init__(self, size):
        """
        Initializes the handler
        """
        logging.Handler.__init__(self)
        self.size = size
        self.store = []

    def emit(self, record):
        """
        Emit a record by adding it to our private queue
        """
        if len(self.store) >= self.size:
            # Loose the oldest record
            self.store.pop(0)
        self.store.append(self.format(record))

    def clear(self):
        self.store = []

    def count(self):
        return len(self.store)

    def last(self):
        if self.store:
            return self.store[len(self.store)-1]
        else:
            return ""

    def content(self):
        """
        Return an array with last records
        """
        return self.store


#------------------------------------------------------------------------------

def print_help():
    print
    print "Usage: %s [-f <configfile>] <other options>" % sabnzbd.MY_NAME
    print
    print "Options marked [*] are stored in the config file"
    print
    print "Options:"
    print "  -f  --config-file <ini>  Location of config file"
    print "  -s  --server <srv:port>  Listen on server:port [*]"
    print "  -t  --templates <templ>  Template directory [*]"
    print "  -2  --template2 <templ>  Secondary template dir [*]"
    print
    print "  -l  --logging <0..2>     Set logging level (0= least, 2= most) [*]"
    print "  -w  --weblogging <0..2>  Set cherrypy logging (0= off, 1= on, 2= file-only) [*]"
    print
    print "  -b  --browser <0..1>     Auto browser launch (0= off, 1= on) [*]"
    if sabnzbd.WIN32:
        print "  -d  --daemon             Use when run as a service"
    else:
        print "  -d  --daemon             Fork daemon process"
    print
    print "      --force              Discard web-port timeout (see Wiki!)"
    print "  -h  --help               Print this message"
    print "  -v  --version            Print version information"
    print "  -c  --clean              Remove queue, cache and logs"
    print "  -p  --pause              Start in paused mode"
    print "      --https <port>       Port to use for HTTPS server"

def print_version():
    print """
%s-%s

Copyright (C) 2008-2009, The SABnzbd-Team <team@sabnzbd.org>
SABnzbd comes with ABSOLUTELY NO WARRANTY.
This is free software, and you are welcome to redistribute it
under certain conditions. It is licensed under the
GNU GENERAL PUBLIC LICENSE Version 2 or (at your option) any later version.

""" % (sabnzbd.MY_NAME, sabnzbd.__version__)


#------------------------------------------------------------------------------
def daemonize():
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError:
        print "fork() failed"
        sys.exit(1)

    os.chdir(sabnzbd.DIR_PROG)
    os.setsid()
    # Make sure I can read my own files and shut out others
    prev= os.umask(0)
    os.umask(prev and int('077',8))

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError:
        print "fork() failed"
        sys.exit(1)

    dev_null = file('/dev/null', 'r')
    os.dup2(dev_null.fileno(), sys.stdin.fileno())

#------------------------------------------------------------------------------
def Bail_Out(browserhost, cherryport, access=False):
    """Abort program because of CherryPy troubles
    """
    logging.error(T('error-noWebUi'))
    if access:
        panic_xport(browserhost, cherryport)
    else:
        panic_port(browserhost, cherryport)
    sabnzbd.halt()
    exit_sab(2)

#------------------------------------------------------------------------------
def Web_Template(key, defweb, wdir):
    """ Determine a correct web template set,
        return full template path
    """
    if wdir is None:
        try:
            wdir = key.get()
        except:
            wdir = ''
    if not wdir:
        wdir = defweb
    key.set(wdir)
    if not wdir:
        # No default value defined, accept empty path
        return ''

    full_dir = real_path(sabnzbd.DIR_INTERFACES, wdir)
    full_main = real_path(full_dir, DEF_MAIN_TMPL)
    logging.info("Web dir is %s", full_dir)

    if not os.path.exists(full_main):
        logging.warning(T('warn-noSkin@1'), full_main)
        full_dir = real_path(sabnzbd.DIR_INTERFACES, DEF_STDINTF)
        full_main = real_path(full_dir, DEF_MAIN_TMPL)
        if not os.path.exists(full_main):
            logging.exception('Cannot find standard template: %s', full_dir)
            panic_tmpl(full_dir)
            exit_sab(1)

    sabnzbd.lang.install_language(real_path(full_dir, DEF_LANGUAGE), sabnzbd.cfg.LANGUAGE.get(), wdir)

    return real_path(full_dir, "templates")


#------------------------------------------------------------------------------
def CheckColor(color, web_dir):
    """ Check existence of color-scheme """
    if color and os.path.exists(os.path.join(web_dir,'static/stylesheets/colorschemes/'+color+'.css')):
        return color
    else:
        return ''

#------------------------------------------------------------------------------
def fix_webname(name):
    xname = name.title()
    if xname in ('Default', 'Plush'):
        return xname
    elif xname in ('Smpl', 'Wizard'):
        return name.lower()
    else:
        return name

#------------------------------------------------------------------------------
def GetProfileInfo(vista):
    """ Get the default data locations
    """
    ok = False
    if sabnzbd.DAEMON:
        # In daemon mode, do not try to access the user profile
        # just assume that everything defaults to the program dir
        sabnzbd.DIR_APPDATA = sabnzbd.DIR_PROG
        sabnzbd.DIR_LCLDATA = sabnzbd.DIR_PROG
        sabnzbd.DIR_HOME = sabnzbd.DIR_PROG
        if sabnzbd.WIN32:
            # Ignore Win32 "logoff" signal
            # This should work, but it doesn't
            # Instead the signal_handler will ignore the "logoff" signal
            #signal.signal(5, signal.SIG_IGN)
            pass
        ok = True
    elif sabnzbd.WIN32:
        specials = get_user_shellfolders()
        try:
            sabnzbd.DIR_APPDATA = '%s\\%s' % (specials['AppData'], DEF_WORKDIR)
            sabnzbd.DIR_LCLDATA = '%s\\%s' % (specials['Local AppData'], DEF_WORKDIR)
            sabnzbd.DIR_HOME = specials['Personal']
            ok = True
        except:
            try:
                if vista:
                    root = os.environ['AppData']
                    user = os.environ['USERPROFILE']
                    sabnzbd.DIR_APPDATA = '%s\\%s' % (root.replace('\\Roaming', '\\Local'), DEF_WORKDIR)
                    sabnzbd.DIR_HOME    = '%s\\Documents' % user
                else:
                    root = os.environ['USERPROFILE']
                    sabnzbd.DIR_APPDATA = '%s\\%s' % (root, DEF_WORKDIR)
                    sabnzbd.DIR_HOME = root

                try:
                    # Conversion to 8bit ASCII required for CherryPy
                    sabnzbd.DIR_APPDATA = sabnzbd.DIR_APPDATA.encode('latin-1')
                    sabnzbd.DIR_HOME = sabnzbd.DIR_HOME.encode('latin-1')
                    ok = True
                except:
                    # If unconvertible characters exist, use MSDOS name
                    try:
                        sabnzbd.DIR_APPDATA = win32api.GetShortPathName(sabnzbd.DIR_APPDATA)
                        sabnzbd.DIR_HOME = win32api.GetShortPathName(sabnzbd.DIR_HOME)
                        ok = True
                    except:
                        pass
                sabnzbd.DIR_LCLDATA = sabnzbd.DIR_APPDATA
            except:
                pass

    elif sabnzbd.DARWIN:
        sabnzbd.DIR_APPDATA = '%s/Library/Application Support/SABnzbd' % (os.environ['HOME'])
        sabnzbd.DIR_LCLDATA = sabnzbd.DIR_APPDATA
        sabnzbd.DIR_HOME = os.environ['HOME']
        ok = True

    else:
        # Unix/Linux
        sabnzbd.DIR_APPDATA = '%s/.%s' % (os.environ['HOME'], DEF_WORKDIR)
        sabnzbd.DIR_LCLDATA = sabnzbd.DIR_APPDATA
        sabnzbd.DIR_HOME = os.environ['HOME']
        ok = True

    if not ok:
        panic("Cannot access the user profile.",
              "Please start with sabnzbd.ini file in another location")
        exit_sab(2)


#------------------------------------------------------------------------------
def print_modules():
    """ Log all detected optional or external modules
    """
    if sabnzbd.decoder.HAVE_YENC:
        logging.info("_yenc module... found!")
    else:
        if hasattr(sys, "frozen"):
            logging.error(T('error-noYEnc'))
        else:
            logging.info("_yenc module... NOT found!")

    if sabnzbd.newsunpack.PAR2_COMMAND:
        logging.info("par2 binary... found (%s)", sabnzbd.newsunpack.PAR2_COMMAND)
    else:
        logging.error(T('error-noPar2'))

    if sabnzbd.newsunpack.PAR2C_COMMAND:
        logging.info("par2-classic binary... found (%s)", sabnzbd.newsunpack.PAR2C_COMMAND)

    if sabnzbd.newsunpack.RAR_COMMAND:
        logging.info("unrar binary... found (%s)", sabnzbd.newsunpack.RAR_COMMAND)
    else:
        logging.warning(T('warn-noUnrar'))

    if sabnzbd.newsunpack.ZIP_COMMAND:
        logging.info("unzip binary... found (%s)", sabnzbd.newsunpack.ZIP_COMMAND)
    else:
        logging.warning(T('warn-noUnzip'))

    if not sabnzbd.WIN32:
        if sabnzbd.newsunpack.NICE_COMMAND:
            logging.info("nice binary... found (%s)", sabnzbd.newsunpack.NICE_COMMAND)
        else:
            logging.info("nice binary... NOT found!")
        if sabnzbd.newsunpack.IONICE_COMMAND:
            logging.info("ionice binary... found (%s)", sabnzbd.newsunpack.IONICE_COMMAND)
        else:
            logging.info("ionice binary... NOT found!")

    if sabnzbd.newswrapper.HAVE_SSL:
        logging.info("pyOpenSSL... found (%s)", sabnzbd.newswrapper.HAVE_SSL)
    else:
        logging.info("pyOpenSSL... NOT found - try apt-get install python-pyopenssl (SSL is optional)")


#------------------------------------------------------------------------------
def get_webhost(cherryhost, cherryport, https_port):
    """ Determine the webhost address and port,
        return (host, port, browserhost)
    """
    if cherryhost is None:
        cherryhost = sabnzbd.cfg.CHERRYHOST.get()
    else:
        sabnzbd.cfg.CHERRYHOST.set(cherryhost)

    # Get IP address, but discard APIPA/IPV6
    # If only APIPA's or IPV6 are found, fall back to localhost
    ipv4 = ipv6 = False
    localhost = hostip = 'localhost'
    try:
        info = socket.getaddrinfo(socket.gethostname(), None)
    except:
        # Hostname does not resolve, use 0.0.0.0
        cherryhost = '0.0.0.0'
        info = socket.getaddrinfo(localhost, None)
    for item in info:
        ip = item[4][0]
        if ip.find('169.254.') == 0:
            pass # Is an APIPA
        elif ip.find(':') >= 0:
            ipv6 = True
        elif ip.find('.') >= 0:
            ipv4 = True
            hostip = ip
            break

    # A blank host will use the local ip address
    if cherryhost == '':
        if ipv6 and ipv4:
            # To protect Firefox users, use numeric IP
            cherryhost = hostip
            browserhost = hostip
        else:
            cherryhost = socket.gethostname()
            browserhost = cherryhost

    # 0.0.0.0 will listen on all ipv4 interfaces (no ipv6 addresses)
    elif cherryhost == '0.0.0.0':
        # Just take the gamble for this
        cherryhost = '0.0.0.0'
        browserhost = localhost

    # :: will listen on all ipv6 interfaces (no ipv4 addresses)
    elif cherryhost in ('::','[::]'):
        cherryhost = cherryhost.strip('[').strip(']')
        # Assume '::1' == 'localhost'
        browserhost = localhost

    # IPV6 address
    elif cherryhost.find('[') >= 0 or cherryhost.find(':') >= 0:
        browserhost = cherryhost

    # IPV6 numeric address
    elif cherryhost.replace('.', '').isdigit():
        # IPV4 numerical
        browserhost = cherryhost

    elif cherryhost == localhost:
        cherryhost = localhost
        browserhost = localhost

    else:
        # If on Vista and/or APIPA, use numerical IP, to help FireFoxers
        if ipv6 and ipv4:
            cherryhost = hostip
        browserhost = cherryhost

    # Some systems don't like brackets in numerical ipv6
    if cherryhost.find('[') >= 0:
        try:
            info = socket.getaddrinfo(cherryhost, None)
        except:
            cherryhost = cherryhost.strip('[]')

    if ipv6 and ipv4 and \
       (browserhost not in ('localhost', '127.0.0.1', '[::1]', '::1')):
        sabnzbd.AMBI_LOCALHOST = True
        logging.info("IPV6 has priority on this system, potential Firefox issue")

    if ipv6 and ipv4 and cherryhost == '' and sabnzbd.WIN32:
        logging.warning(T('warn-0000'))

    if cherryport is None:
        cherryport = sabnzbd.cfg.CHERRYPORT.get_int()
    else:
        sabnzbd.cfg.CHERRYPORT.set(str(cherryport))

    if https_port is None:
        https_port = sabnzbd.cfg.HTTPS_PORT.get_int()
    else:
        sabnzbd.cfg.HTTPS_PORT.set(str(https_port))
        # if the https port was specified, assume they want HTTPS enabling also
        sabnzbd.cfg.ENABLE_HTTPS.set(True)

    if cherryport == https_port:
        sabnzbd.cfg.ENABLE_HTTPS.set(False)
        logging.error(T('error-sameHTTP-HTTPS'))

    return cherryhost, cherryport, browserhost, https_port

def is_sabnzbd_running(url):
    import urllib2
    try:
        url = '%sapi?mode=version' % (url)
        s = urllib2.urlopen(url)
        ver = s.read()
        if ver and ver.strip() == sabnzbd.__version__:
            return True
        else:
            return False
    except:
        return False

def find_free_port(host, currentport, i=0):
    while i >=10 and currentport <= 49151:
        try:
            cherrypy.process.servers.check_port(host, currentport)
            return currentport
        except:
            currentport+=5
            i+=1
    return -1

def check_for_sabnzbd(url, upload_nzbs):
    # Check for a running instance of sabnzbd(same version) on this port
    if is_sabnzbd_running(url):
        # Upload any specified nzb files to the running instance
        if upload_nzbs:
            from sabnzbd.utils.upload import upload_file
            for f in upload_nzbs:
                upload_file(url, f)
        else:
            # Launch the web browser and quit since sabnzbd is already running
            launch_a_browser(url, force=True)
        exit_sab(0)
        return True
    return False

def copy_old_files(newpath):
    # OSX only:
    # If no INI file found but old one exists, copy it
    # When copying the INI, also copy rss, bookmarks and watched-data
    if not os.path.exists(os.path.join(newpath, DEF_INI_FILE)):
        if not os.path.exists(newpath):
            os.mkdir(newpath)
        oldpath = os.environ['HOME'] + "/.sabnzbd"
        oldini = os.path.join(oldpath, DEF_INI_FILE)
        if os.path.exists(oldini):
            import shutil
            try:
                shutil.copy(oldini, newpath)
            except:
                pass
            oldpath = os.path.join(oldpath, DEF_CACHE_DIR)
            newpath = os.path.join(newpath, DEF_CACHE_DIR)
            if not os.path.exists(newpath):
                os.mkdir(newpath)
            try:
                shutil.copy(os.path.join(oldpath, RSS_FILE_NAME), newpath)
            except:
                pass
            try:
                shutil.copy(os.path.join(oldpath, BOOKMARK_FILE_NAME), newpath)
            except:
                pass
            try:
                shutil.copy(os.path.join(oldpath, SCAN_FILE_NAME), newpath)
            except:
                pass


def evaluate_inipath(path):
    # Derive INI file path from a partial path.
    # Full file path: if file does not exist the name must contain a dot
    # but not a leading dot.
    # A foldername is enough, the standard name will be appended.

    path = os.path.normpath(os.path.abspath(path))
    inipath = os.path.join(path, DEF_INI_FILE)
    if os.path.isdir(path):
        return inipath
    elif os.path.isfile(path):
        return path
    else:
        dir, name = os.path.split(path)
        if name.find('.') < 1:
            return inipath
        else:
            return path


def cherrypy_logging(log_path):
    log = cherrypy.log
    log.access_file = ''
    log.error_file = ''
    # Max size of 512KB
    maxBytes = getattr(log, "rot_maxBytes", 524288)
    # cherrypy.log cherrypy.log.1 cherrypy.log.2
    backupCount = getattr(log, "rot_backupCount", 3)

    # Make a new RotatingFileHandler for the error log.
    fname = getattr(log, "rot_error_file", log_path)
    h = logging.handlers.RotatingFileHandler(fname, 'a', maxBytes, backupCount)
    h.setLevel(logging.DEBUG)
    h.setFormatter(cherrypy._cplogging.logfmt)
    log.error_log.addHandler(h)
    '''
    # Make a new RotatingFileHandler for the access log.
    fname = getattr(log, "rot_access_file", "access.log")
    h = handlers.RotatingFileHandler(fname, 'a', maxBytes, backupCount)
    h.setLevel(logging.DEBUG)
    h.setFormatter(cherrypy._cplogging.logfmt)
    log.access_log.addHandler(h)
    '''

#------------------------------------------------------------------------------
def main():
    global LOG_FLAG

    AUTOBROWSER = None
    testlog = False # Allow log options for test-releases

    sabnzbd.MY_FULLNAME = os.path.normpath(os.path.abspath(sys.argv[0]))
    sabnzbd.MY_NAME = os.path.basename(sabnzbd.MY_FULLNAME)
    sabnzbd.DIR_PROG = os.path.dirname(sabnzbd.MY_FULLNAME)
    sabnzbd.DIR_INTERFACES = real_path(sabnzbd.DIR_PROG, DEF_INTERFACES)
    sabnzbd.DIR_LANGUAGE = real_path(sabnzbd.DIR_PROG, DEF_LANGUAGE)

    if getattr(sys, 'frozen', None) == 'macosx_app':
        # Correct path if frozen with py2app (OSX)
        sabnzbd.MY_FULLNAME = sabnzbd.MY_FULLNAME.replace("/Resources/SABnzbd.py","/MacOS/SABnzbd")

    # Need console logging for SABnzbd.py and SABnzbd-console.exe
    consoleLogging = (not hasattr(sys, "frozen")) or (sabnzbd.MY_NAME.lower().find('-console') > 0)

    # No console logging needed for OSX app
    noConsoleLoggingOSX = (sabnzbd.DIR_PROG.find('.app/Contents/Resources') > 0)
    if noConsoleLoggingOSX:
        consoleLogging = 1

    LOGLEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)

    # Setup primary logging to prevent default console logging
    gui_log = guiHandler(MAX_WARNINGS)
    gui_log.setLevel(logging.WARNING)
    format_gui = '%(asctime)s\n%(levelname)s\n%(message)s'
    gui_log.setFormatter(logging.Formatter(format_gui))
    sabnzbd.GUIHANDLER = gui_log

    # Create logger
    logger = logging.getLogger('')
    logger.setLevel(logging.WARNING)
    logger.addHandler(gui_log)

    # Create a list of passed files to load on startup
    # or pass to an already running instance of sabnzbd
    upload_nzbs = []
    for entry in sys.argv:
        if get_ext(entry) in ('.nzb','.zip','.rar', '.nzb.gz'):
            upload_nzbs.append(entry)
            sys.argv.remove(entry)


    try:
        opts, args = getopt.getopt(sys.argv[1:], "phdvncw:l:s:f:t:b:2:",
                                   ['pause', 'help', 'daemon', 'nobrowser', 'clean', 'logging=',
                                    'weblogging=', 'server=', 'templates',
                                    'template2', 'browser=', 'config-file=', 'delay=', 'force',
                                    'version', 'https=', 'testlog'])
    except getopt.GetoptError:
        print_help()
        exit_sab(2)

    fork = False
    pause = False
    inifile = None
    cherryhost = None
    cherryport = None
    https_port = None
    cherrypylogging = None
    clean_up = False
    logging_level = None
    web_dir = None
    web_dir2 = None
    delay = 0.0
    vista = False
    vista64 = False
    force_web = False
    re_argv = [sys.argv[0]]

    for opt, arg in opts:
        if (opt in ('-d', '--daemon')):
            if not sabnzbd.WIN32:
                fork = True
            AUTOBROWSER = False
            sabnzbd.DAEMON = True
            consoleLogging = False
            re_argv.append(opt)
        elif opt in ('-h', '--help'):
            print_help()
            exit_sab(0)
        elif opt in ('-f', '--config-file'):
            inifile = arg
            re_argv.append(opt)
            re_argv.append(arg)
        elif opt in ('-t', '--templates'):
            web_dir = fix_webname(arg)
        elif opt in ('-2', '--template2'):
            web_dir2 = fix_webname(arg)
        elif opt in ('-s', '--server'):
            (cherryhost, cherryport) = split_host(arg)
        elif opt in ('-n', '--nobrowser'):
            AUTOBROWSER = False
        elif opt in ('-b', '--browser'):
            try:
                AUTOBROWSER = bool(int(arg))
            except:
                AUTOBROWSER = True
        elif opt in ('-c', '--clean'):
            clean_up= True
        elif opt in ('-w', '--weblogging'):
            try:
                cherrypylogging = int(arg)
            except:
                cherrypylogging = -1
            if cherrypylogging < 0 or cherrypylogging > 2:
                print_help()
                exit_sab(1)
        elif opt in ('-l', '--logging'):
            try:
                logging_level = int(arg)
            except:
                logging_level = -1
            if logging_level < 0 or logging_level > 2:
                print_help()
                exit_sab(1)
        elif opt in ('-v', '--version'):
            print_version()
            exit_sab(0)
        elif opt in ('-p', '--pause'):
            pause = True
        elif opt in ('--delay'):
            # For debugging of memory leak only!!
            try:
                delay = float(arg)
            except:
                pass
        elif opt in ('--force'):
            force_web = True
            re_argv.append(opt)
        elif opt in ('--https'):
            https_port = int(arg)
            re_argv.append(opt)
            re_argv.append(arg)
        elif opt in ('--testlog'):
            testlog = True
            re_argv.append(opt)

    # Detect Vista or higher
    if sabnzbd.WIN32:
        if platform.platform().find('Windows-32bit') >= 0:
            vista = True
            vista64 = 'ProgramFiles(x86)' in os.environ

    if inifile:
        # INI file given, simplest case
        inifile = evaluate_inipath(inifile)
    else:
        # No ini file given, need profile data
        GetProfileInfo(vista)
        # Find out where INI file is
        inifile = os.path.abspath(sabnzbd.DIR_PROG + '/' + DEF_INI_FILE)
        if not os.path.exists(inifile):
            inifile = os.path.abspath(sabnzbd.DIR_LCLDATA + '/' + DEF_INI_FILE)
            if sabnzbd.DARWIN:
                copy_old_files(sabnzbd.DIR_LCLDATA)

    # If INI file at non-std location, then use program dir as $HOME
    if sabnzbd.DIR_LCLDATA != os.path.dirname(inifile):
        sabnzbd.DIR_HOME = os.path.dirname(inifile)

    # All system data dirs are relative to the place we found the INI file
    sabnzbd.DIR_LCLDATA = os.path.dirname(inifile)

    if not os.path.exists(inifile) and not os.path.exists(sabnzbd.DIR_LCLDATA):
        try:
            os.makedirs(sabnzbd.DIR_LCLDATA)
        except IOError:
            panic('Cannot create folder "%s".' % sabnzbd.DIR_LCLDATA, 'Check specified INI file location.')
            exit_sab(1)

    sabnzbd.cfg.set_root_folders(sabnzbd.DIR_HOME, sabnzbd.DIR_LCLDATA, sabnzbd.DIR_PROG, sabnzbd.DIR_INTERFACES)

    if not config.read_config(inifile):
        panic('"%s" is not a valid configuration file.' % inifile, \
              'Specify a correct file or delete this file.')
        exit_sab(1)

    # Determine web host address
    cherryhost, cherryport, browserhost, https_port = get_webhost(cherryhost, cherryport, https_port)

    # If an instance of sabnzbd(same version) is already running on this port, launch the browser
    # If another program or sabnzbd version is on this port, try 10 other ports going up in a step of 5
    # If 'Port is not bound' (firewall) do not do anything (let the script further down deal with that).
    ## SSL
    enable_https = sabnzbd.cfg.ENABLE_HTTPS.get()
    if enable_https and https_port:
        try:
            cherrypy.process.servers.check_port(browserhost, https_port)
        except IOError, error:
            if str(error) == 'Port not bound.':
                pass
            else:
                url = 'https://%s:%s/' % (browserhost, https_port)
                if not check_for_sabnzbd(url, upload_nzbs):
                    port = find_free_port(browserhost, https_port)
                    if port > 0:
                        sabnzbd.cfg.HTTPS_PORT.set(port)
                        cherryport = port
    ## NonSSL
    try:
        cherrypy.process.servers.check_port(browserhost, cherryport)
    except IOError, error:
        if str(error) == 'Port not bound.':
            pass
        else:
            url = 'http://%s:%s/' % (browserhost, cherryport)
            if not check_for_sabnzbd(url, upload_nzbs):
                port = find_free_port(browserhost, cherryport)
                if port > 0:
                    sabnzbd.cfg.CHERRYPORT.set(port)
                    cherryport = port


    if cherrypylogging is None:
        cherrypylogging = sabnzbd.cfg.LOG_WEB.get()
    else:
        sabnzbd.cfg.LOG_WEB.set(cherrypylogging)

    if logging_level is None:
        logging_level = sabnzbd.cfg.LOG_LEVEL.get()
    else:
        sabnzbd.cfg.LOG_LEVEL.set(logging_level)

    ver, testRelease = convert_version(sabnzbd.__version__)
    if testRelease and not testlog:
        logging_level = 2

    logdir = sabnzbd.cfg.LOG_DIR.get_path()
    if fork and not logdir:
        print "Error:"
        print "I refuse to fork without a log directory!"
        sys.exit(1)

    if clean_up:
        xlist= glob.glob(logdir + '/*')
        for x in xlist:
            if x.find(RSS_FILE_NAME) < 0:
                os.remove(x)

    try:
        sabnzbd.LOGFILE = os.path.join(logdir, DEF_LOG_FILE)
        logsize = sabnzbd.cfg.LOG_SIZE.get_int()
        rollover_log = logging.handlers.RotatingFileHandler(\
            sabnzbd.LOGFILE, 'a+',
            logsize,
            sabnzbd.cfg.LOG_BACKUPS.get())

        format = '%(asctime)s::%(levelname)s::[%(module)s:%(lineno)d] %(message)s'
        rollover_log.setFormatter(logging.Formatter(format))
        rollover_log.addFilter(FilterCP3())
        sabnzbd.LOGHANDLER = rollover_log
        logger.addHandler(rollover_log)
        logger.setLevel(LOGLEVELS[logging_level])

    except IOError:
        print "Error:"
        print "Can't write to logfile"
        exit_sab(2)

    if fork:
        try:
            x= sys.stderr.fileno
            x= sys.stdout.fileno
            ol_path = os.path.join(logdir, DEF_LOG_ERRFILE)
            out_log = file(ol_path, 'a+', 0)
            sys.stderr.flush()
            sys.stdout.flush()
            os.dup2(out_log.fileno(), sys.stderr.fileno())
            os.dup2(out_log.fileno(), sys.stdout.fileno())
        except AttributeError:
            pass

    else:
        try:
            x= sys.stderr.fileno
            x= sys.stdout.fileno

            if consoleLogging:
                console = logging.StreamHandler()
                console.addFilter(FilterCP3())
                console.setLevel(LOGLEVELS[logging_level])
                console.setFormatter(logging.Formatter(format))
                logger.addHandler(console)
            if noConsoleLoggingOSX:
                logging.info('Console logging for OSX App disabled')
                so = file('/dev/null', 'a+')
                os.dup2(so.fileno(), sys.stdout.fileno())
                os.dup2(so.fileno(), sys.stderr.fileno())
        except AttributeError:
            pass

    logging.info('--------------------------------')
    logging.info('%s-%s (rev=%s)', sabnzbd.MY_NAME, sabnzbd.__version__, sabnzbd.__baseline__)
    if sabnzbd.WIN32:
        suffix = ''
        if vista: suffix = ' (=Vista)'
        if vista64: suffix = ' (=Vista64)'
        logging.info('Platform=%s%s Class=%s', platform.platform(), suffix, os.name)
    else:
        logging.info('Platform = %s', os.name)
    logging.info('Python-version = %s', sys.version)

    if testRelease:
        logging.info('Test release, setting maximum logging levels')

    # OSX 10.5 I/O priority setting
    if sabnzbd.DARWIN:
        logging.info('[osx] IO priority setting')
        try:
            from ctypes import cdll
            libc=cdll.LoadLibrary('/usr/lib/libc.dylib')
            boolSetResult=libc.setiopolicy_np(0,1,3)
            logging.info('[osx] IO priority set to throttle for process scope')
        except:
            logging.info('[osx] IO priority setting not supported')
            pass

    if AUTOBROWSER != None:
        sabnzbd.cfg.AUTOBROWSER.set(AUTOBROWSER)
    else:
        AUTOBROWSER = sabnzbd.cfg.AUTOBROWSER.get()

    sabnzbd.cfg.DEBUG_DELAY.set(delay)

    # Find external programs
    sabnzbd.newsunpack.find_programs(sabnzbd.DIR_PROG)
    print_modules()

    init_ok = sabnzbd.initialize(pause, clean_up, evalSched=True)

    if not init_ok:
        logging.error(T('error-noStartup@2'),
                      sabnzbd.MY_NAME, sabnzbd.__version__)
        exit_sab(2)

    os.chdir(sabnzbd.DIR_PROG)

    web_dir  = Web_Template(sabnzbd.cfg.WEB_DIR,  DEF_STDINTF,  web_dir)
    web_dir2 = Web_Template(sabnzbd.cfg.WEB_DIR2, '', web_dir2)

    wizard_dir = os.path.join(sabnzbd.DIR_INTERFACES, 'wizard')
    sabnzbd.lang.install_language(os.path.join(wizard_dir, DEF_LANGUAGE), sabnzbd.cfg.LANGUAGE.get(), 'wizard')

    sabnzbd.WEB_DIR  = web_dir
    sabnzbd.WEB_DIR2 = web_dir2
    sabnzbd.WIZARD_DIR = wizard_dir

    sabnzbd.WEB_COLOR = CheckColor(sabnzbd.cfg.WEB_COLOR.get(),  web_dir)
    sabnzbd.cfg.WEB_COLOR.set(sabnzbd.WEB_COLOR)
    sabnzbd.WEB_COLOR2 = CheckColor(sabnzbd.cfg.WEB_COLOR2.get(),  web_dir2)
    sabnzbd.cfg.WEB_COLOR2.set(sabnzbd.WEB_COLOR2)

    if fork and not sabnzbd.WIN32:
        daemonize()

    # Save the INI file
    config.save_config(force=True)

    logging.info('Starting %s-%s', sabnzbd.MY_NAME, sabnzbd.__version__)
    try:
        sabnzbd.start()
    except:
        logging.exception("Failed to start %s-%s", sabnzbd.MY_NAME, sabnzbd.__version__)
        sabnzbd.halt()

    # Upload any nzb/zip/rar/nzb.gz files from file association
    if upload_nzbs:
        from sabnzbd.utils.upload import add_local
        for f in upload_nzbs:
            add_local(f)

    cherrylogtoscreen = False
    sabnzbd.WEBLOGFILE = None

    if cherrypylogging:
        if logdir:
            sabnzbd.WEBLOGFILE = os.path.join(logdir, DEF_LOG_CHERRY)
        # Define our custom logger for cherrypy errors
        cherrypy_logging(sabnzbd.WEBLOGFILE)
        if not fork:
            try:
                x= sys.stderr.fileno
                x= sys.stdout.fileno
                if cherrypylogging == 1:
                    cherrylogtoscreen = True
            except:
                pass

    cherrypy.config.update({'server.environment': 'production',
                            'server.socket_host': cherryhost,
                            'server.socket_port': cherryport,
                            'log.screen': cherrylogtoscreen,
                            'engine.autoreload_frequency' : 100,
                            'engine.autoreload_on' : False,
                            'engine.reexec_retry' : 100,
                            'tools.encode.on' : True,
                            'tools.gzip.on' : True,
                            'tools.sessions.on' : True,
                            'request.show_tracebacks': True,
                            'checker.check_localhost' : bool(consoleLogging),
                            'error_page.401': sabnzbd.misc.error_page_401
                           })

    https_cert = sabnzbd.cfg.HTTPS_CERT.get_path()
    https_key = sabnzbd.cfg.HTTPS_KEY.get_path()
    if enable_https:
        # If either the HTTPS certificate or key do not exist, make some self-signed ones.
        if not (https_cert and os.path.exists(https_cert)) or not (https_key and os.path.exists(https_key)):
            create_https_certificates(https_cert, https_key)

        if https_port and not (os.path.exists(https_cert) or os.path.exists(https_key)):
            logging.warning(T('warn-noCertKey'))
            https_port = False

        if https_port:
            secure_server = _cpwsgi_server.CPWSGIServer()
            secure_server.bind_addr = (cherryhost, https_port)
            secure_server.ssl_certificate = https_cert
            secure_server.ssl_private_key = https_key
            adapter = _cpserver.ServerAdapter(cherrypy.engine, secure_server, secure_server.bind_addr)
            adapter.subscribe()

    static = {'tools.staticdir.on': True, 'tools.staticdir.dir': os.path.join(web_dir, 'static')}
    wizard_static = {'tools.staticdir.on': True, 'tools.staticdir.dir': os.path.join(wizard_dir, 'static')}

    appconfig = {'/sabnzbd/api' : {'tools.basic_auth.on' : False},
                 '/api' : {'tools.basic_auth.on' : False},
                 '/m/api' : {'tools.basic_auth.on' : False},
                 '/sabnzbd/shutdown': {'streamResponse': True},
                 '/sabnzbd/static': static,
                 '/static': static,
                 '/sabnzbd/wizard/static': wizard_static,
                 '/wizard/static': wizard_static
                 }

    if web_dir2:
        static2 = {'tools.staticdir.on': True, 'tools.staticdir.dir': os.path.join(web_dir2, 'static')}
        appconfig['/sabnzbd/m/api'] = {'tools.basic_auth.on' : False}
        appconfig['/sabnzbd/m/shutdown'] = {'streamResponse': True}
        appconfig['/sabnzbd/m/static'] = static2
        appconfig['/m/static'] = static2
        appconfig['/sabnzbd/m/wizard/static'] = wizard_static
        appconfig['/m/wizard/static'] = wizard_static

    login_page = sabnzbd.interface.MainPage(web_dir, '/', web_dir2, '/m/', first=2)
    cherrypy.tree.mount(login_page, '/', config=appconfig)

    # Set authentication for CherryPy
    sabnzbd.interface.set_auth(cherrypy.config)

    logging.info('Starting web-interface on %s:%s', cherryhost, cherryport)

    sabnzbd.cfg.LOG_LEVEL.callback(guard_loglevel)

    try:
        # Use internal cherrypy check first to prevent ugly tracebacks
        cherrypy.process.servers.check_port(browserhost, cherryport)
        cherrypy.engine.start()
    except IOError, error:
        if str(error) == 'Port not bound.':
            if not force_web:
                panic_fwall(vista)
                sabnzbd.halt()
                exit_sab(2)
        else:
            logging.debug("Failed to start web-interface: ", exc_info = True)
            Bail_Out(browserhost, cherryport)
    except socket.error, error:
        logging.debug("Failed to start web-interface: ", exc_info = True)
        Bail_Out(browserhost, cherryport, access=True)
    except:
        logging.debug("Failed to start web-interface: ", exc_info = True)
        Bail_Out(browserhost, cherryport)

    # Wait for server to become ready
    cherrypy.engine.wait(cherrypy.process.wspbus.states.STARTED)

    if enable_https and https_port:
        launch_a_browser("https://%s:%s/sabnzbd" % (browserhost, https_port))
    else:
        launch_a_browser("http://%s:%s/sabnzbd" % (browserhost, cherryport))

    notify("SAB_Launched", None)
    osx.sendGrowlMsg('SABnzbd %s' % (sabnzbd.__version__),"http://%s:%s/sabnzbd" % (browserhost, cherryport),osx.NOTIFICATION['startup'])
    # Now's the time to check for a new version
    check_latest_version()

    # Have to keep this running, otherwise logging will terminate
    timer = 0
    while not sabnzbd.SABSTOP:
        time.sleep(3)

        # Check for loglevel changes, ignore for non-final releases
        if LOG_FLAG and (testlog or not testRelease):
            LOG_FLAG = False
            level = LOGLEVELS[sabnzbd.cfg.LOG_LEVEL.get()]
            logger.setLevel(level)
            if consoleLogging:
                console.setLevel(level)

        ### 30 sec polling tasks
        if timer > 9:
            timer = 0
            # Keep Windows awake (if needed)
            sabnzbd.keep_awake()
            # Restart scheduler (if needed)
            scheduler.restart()
            # Save config (if needed)
            config.save_config()
        else:
            timer += 1

        ### 3 sec polling tasks
        # Check for auto-restart request
        if cherrypy.engine.execv:
            scheduler.stop()
            sabnzbd.halt()
            cherrypy.engine.exit()
            sabnzbd.SABSTOP = True
            if downloader.paused():
                re_argv.append('-p')
            sys.argv = re_argv
            if sabnzbd.DARWIN:
                args = sys.argv[:]
                args.insert(0, sys.executable)
                #TO FIX : when executing from sources on osx, after a restart, process is detached from console
                #If OSX frozen restart of app instead of embedded python
                if getattr(sys, 'frozen', None) == 'macosx_app':
                    #[[NSProcessInfo processInfo] processIdentifier]]
                    #logging.info("%s" % (NSProcessInfo.processInfo().processIdentifier()))
                    logging.info(os.getpid())
                    os.system('kill -9 %s && open "%s"' % (os.getpid(),sabnzbd.MY_FULLNAME.replace("/Contents/MacOS/SABnzbd","")) )
                else:
                    pid = os.fork()
                    if pid == 0:
                        os.execv(sys.executable, args)
            else:
                cherrypy.engine._do_execv()

    config.save_config()

    notify("SAB_Shutdown", None)
    osx.sendGrowlMsg('SABnzbd',T('grwl-shutdown-end-msg'),osx.NOTIFICATION['startup'])
    logging.info('Leaving SABnzbd')
    sys.stderr.flush()
    sys.stdout.flush()
    if getattr(sys, 'frozen', None) == 'macosx_app':
        AppHelper.stopEventLoop()
    else:
        os._exit(0)


#####################################################################
#
# Platform specific startup code
#

if not sabnzbd.DARWIN:

    # Windows & Unix/Linux

    if __name__ == '__main__':
        main()

else:

    # OSX

    if __name__ == '__main__':
        try:
            from PyObjCTools import AppHelper
            from SABnzbdDelegate import SABnzbdDelegate

            class startApp(Thread):
                def __init__(self):
                    logging.info('[osx] sabApp Starting - starting main thread')
                    Thread.__init__(self)
                def run(self):
                    main()
                    logging.info('[osx] sabApp Stopping - main thread quit ')
                    AppHelper.stopEventLoop()
                def stop(self):
                    logging.info('[osx] sabApp Quit - stopping main thread ')
                    sabnzbd.halt()
                    cherrypy.engine.exit()
                    sabnzbd.SABSTOP = True
                    logging.info('[osx] sabApp Quit - main thread stopped')

            sabApp = startApp()
            sabApp.start()
            AppHelper.runEventLoop()

        except:
            main()

