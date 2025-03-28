#!/usr/bin/env python3
#
# Copyright (c) 2015 Jon Turney
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

#
# utilities for working with a maintainer list
#

#
# things we know about a maintainer:
#
# - their home directory
# - the list of packages they maintain (given by cygwin-pkg-list)
# - an email address (in HOME/!email (or !mail), as we don't want to publish
#   it, and want to allow the maintainer to change it)
# - the timestamp when 'ignoring' warnings were last emitted
# - the timestamp of their last ssh connection
#

import logging
import os
import re
from collections import UserString

from . import utils


#
# MaintainerPackage object behaves like a string of the package name, but also
# supports is_orphaned() and maintainers() methods
#
class MaintainerPackage(UserString):
    def __init__(self, name, maintainers, groups, orphaned):
        super().__init__(name)
        self._maintainers = maintainers
        self._groups = groups
        self._orphaned = orphaned

    # XXX: for historical reasons, 'ORPHANED' still appears in the maintainer
    # list, when this is True.  Probably should fix that...
    def is_orphaned(self):
        return self._orphaned

    def maintainers(self):
        return self._maintainers

    def groups(self):
        return self._groups


#
#
#


class Maintainer(object):
    _homedirs = ''

    def __init__(self, name, email=None, pkgs=None):
        if email is None:
            email = []
        if pkgs is None:
            pkgs = []

        self.name = name
        self.email = email
        self.pkgs = pkgs
        self.quiet = False
        self.has_homedir = os.path.isdir(self.homedir())

        # the mtime of this file records the timestamp
        reminder_file = os.path.join(self.homedir(), '!reminder-timestamp')
        if os.path.exists(reminder_file):
            self.reminder_time = os.path.getmtime(reminder_file)
        else:
            self.reminder_time = 0
        self.reminders_issued = False
        self.reminders_timestamp_checked = False

        # the mtime of this file records the last ssh session
        last_seen_file = os.path.join(self.homedir(), '.last-seen')
        if os.path.isfile(last_seen_file):
            self.last_seen = os.path.getmtime(last_seen_file)
        else:
            self.last_seen = 0  # meaning 'unknown'

    def __repr__(self):
        return "maintainers.Maintainer('%s', %s, %s)" % (self.name, self.email, self.pkgs)

    def homedir(self):
        return os.path.join(Maintainer._homedirs, self.name)

    def _update_reminder_time(self):
        reminder_file = os.path.join(self.homedir(), '!reminder-timestamp')

        if self.reminders_issued:
            # if reminders were issued, update the timestamp
            logging.debug("updating reminder time for %s" % self.name)
            utils.touch(reminder_file)
        elif (not self.reminders_timestamp_checked) and (self.reminder_time != 0):
            # if we didn't need to check the reminder timestamp, it can be
            # reset
            logging.debug("resetting reminder time for %s" % self.name)
            try:
                os.remove(reminder_file)
            except FileNotFoundError:
                pass

    @staticmethod
    def _find(mlist, name):
        mlist.setdefault(name, Maintainer(name))
        return mlist[name]


# add maintainers which have existing directories
def add_directories(mlist, homedirs):
    Maintainer._homedirs = homedirs

    for n in os.listdir(homedirs):
        if not os.path.isdir(os.path.join(homedirs, n)):
            continue

        m = Maintainer._find(mlist, n)

        # !mail is the deprecated historical alternative
        for e in ['!email', '!mail']:
            email = os.path.join(homedirs, m.name, e)
            if os.path.isfile(email):
                with open(email) as f:
                    for l in f:
                        # one address per line, ignore blank and comment lines
                        if l.startswith('#'):
                            continue
                        l = l.strip()
                        if l.lower() == 'quiet':
                            m.quiet = True
                        elif l:
                            m.email.append(l)

    return mlist


# add maintainers from the package maintainers list, with the packages they
# maintain
@utils.mtime_cache
def _read_pkglist(pkglist):
    mpkgs = {}
    teams = {}

    with open(pkglist) as f:
        for (i, l) in enumerate(f):

            def _split_maintainer_names(m, teams=None):
                # joint maintainers are separated by '/'
                maintainers = list()
                groups = list()

                for name in m.split('/'):
                    if not name:
                        continue

                    name = name.strip()

                    # is the maintainer name ascii?
                    #
                    # (despite containing spaces, think of these as an account
                    # name, rather than a display name)
                    try:
                        name.encode('ascii')
                    except UnicodeError:
                        logging.error("non-ascii maintainer name '%s' in %s:%d: '%s', skipped" % (name, pkglist, i, l))
                        continue

                    if name.startswith('@'):
                        groups.append(name[1:])

                        if teams and name in teams:
                            for n in teams[name]:
                                if n not in maintainers:
                                    maintainers.append(n)
                        else:
                            logging.error("unknown team '%s' in %s:%d: '%s', skipped" % (name, pkglist, i, l))
                    else:
                        # avoid adding name if it's already in the list (a set
                        # is not appropriate here, as we have the concept of
                        # 'first named maintainer'
                        if name not in maintainers:
                            maintainers.append(name)

                return maintainers, groups

            if l.startswith('#'):
                # comment
                continue
            elif l.startswith('@'):
                # 'team' definition of the form '@<team> <maintainer(s)>'
                match = re.match(r'^(\S+)\s+(.+)$', l)
                if match:
                    team = match.group(1)
                    rest = match.group(2)

                    teams[team], _ = _split_maintainer_names(rest)
                    continue
            else:
                # package
                orphaned = False
                l = l.rstrip()

                # match lines of the form '<package> <maintainer(s)|status>'
                match = re.match(r'^(\S+)\s+(.+)$', l)
                if match:
                    pkg = match.group(1)
                    rest = match.group(2)

                    # does rest starts with a status in all caps?
                    status_match = re.match(r'^([A-Z]{2,})\b.*$', rest)
                    if status_match:
                        status = status_match.group(1)

                        # packages marked as 'OBSOLETE' are obsolete
                        if status == 'OBSOLETE':
                            # obsolete packages have no maintainer
                            #
                            # XXX: perhaps disallow even trusties to upload (or
                            # warn if they try?)
                            m = ''

                        # orphaned packages are assigned to 'ORPHANED'
                        elif status == 'ORPHANED':
                            m = status
                            orphaned = True

                            # also add any previous maintainer(s) listed
                            prevm = re.match(r'^ORPHANED\s\((.*)\)', rest)
                            if prevm:
                                m = m + '/' + prevm.group(1)
                        else:
                            logging.error("unknown package status '%s' in line %s:%d: '%s'" % (status, pkglist, i, l))
                            continue
                    else:
                        m = rest

                    maintainers, groups = _split_maintainer_names(m, teams)

                    mpkgs[pkg] = MaintainerPackage(pkg, maintainers, groups, orphaned)
                    continue

            # couldn't handle the line
            logging.error("unrecognized line in %s:%d: '%s'" % (pkglist, i, l))

    return mpkgs


#
def pkg_list(pkglist):
    return _read_pkglist(pkglist)


# create maintainer list
def maintainer_list(args):
    Maintainer._homedirs = args.homedir

    mlist = {}

    # add all maintainers for all packages
    for p in pkg_list(args.pkglist).values():
        for m in p.maintainers():
            Maintainer._find(mlist, m).pkgs.append(p)

    # read information from homedirs
    mlist = add_directories(mlist, args.homedir)

    # check all maintainers have an email
    for m in mlist.values():
        if m.name == 'ORPHANED':
            continue

        # not required if only a previous maintainer for some orphaned packages
        if all(p.is_orphaned() for p in m.pkgs):
            continue

        if not m.email:
            logging.error("no email address known for maintainer '%s'" % (m.name))

    return mlist


def update_reminder_times(mlist):
    for m in mlist.values():
        m._update_reminder_time()


# a list of all packages
def all_packages(pkglist):
    return pkg_list(pkglist).keys()


#
#
#

if __name__ == "__main__":
    from . import common_constants
    p = pkg_list(common_constants.PKGMAINT)
    print(p['xwininfo'].maintainers())
