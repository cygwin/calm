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

import logging
import os
from collections import defaultdict

from . import logfilters
from . import utils


#
# movelist class
#

class MoveList(object):
    def __init__(self, basedir=None):
        # a movelist is a dict with relative directory paths for keys and a list
        # of filenames for each value
        self.movelist = defaultdict(list)
        # the directory the paths are relative to (by default the 'relarea')
        if basedir:
            self.basedir = basedir

    def __len__(self):
        return len(self.movelist)

    def __bool__(self):
        # empty movelists are false
        return len(self.movelist) > 0

    def add(self, relpath, f):
        self.movelist[relpath].append(f)

    def remove(self, relpath):
        del self.movelist[relpath]

    def _move(self, args, fromdir, todir, verb):
        for p in sorted(self.movelist):
            # a clunky way of determining the package which owns these files
            package = p.split(os.sep)[2]
            with logfilters.AttrFilter(package=package):
                logging.debug("mkdir %s" % os.path.join(todir, p))
                if not args.dryrun:
                    utils.makedirs(os.path.join(todir, p))
                logging.debug("move from '%s' to '%s':" % (os.path.join(fromdir, p), os.path.join(todir, p)))
                for f in sorted(self.movelist[p]):
                    if os.path.exists(os.path.join(fromdir, p, f)):
                        logging.info("%sing %s" % (verb, os.path.join(p, f)))
                        if not args.dryrun:
                            os.rename(os.path.join(fromdir, p, f), os.path.join(todir, p, f))
                    else:
                        logging.error("can't %s %s, as it doesn't exist" % (verb, f))

    def move_to_relarea(self, m, args, desc):
        if self.movelist:
            logging.info("move from %s's %s area to release area:" % (m.name, desc))
        self._move(args, self.basedir, args.rel_area, 'deploy')

    def move_to_vault(self, args):
        if self.movelist:
            logging.info("move from release area to vault:")
        self._move(args, args.rel_area, args.vault, 'vault')

    # apply a function to all files in the movelists
    def map(self, function):
        for p in self.movelist:
            for f in self.movelist[p]:
                function(p, f)

    # compute the intersection of a pair of movelists
    @staticmethod
    def intersect(a, b):
        i = MoveList()
        for p in a.movelist.keys() & b.movelist.keys():
            pi = set(a.movelist[p]) & set(b.movelist[p])
            if pi:
                i.movelist[p] = pi
        return i
