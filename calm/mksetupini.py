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
# mksetupini
#
# Make a setup.ini file from a collection of tarfiles and .hint files
# (this is intended to be a replacement for genini)
#

import argparse
import logging
import os
import sys

from . import common_constants
from . import hint
from . import package

try:
    import spelling
except ImportError:
    pass


#
#
#
def do_main(args):
    # build package list
    packages = package.read_packages(args.rel_area, args.arch)

    # spellcheck text hints
    if args.spell:
        if spelling:
            spelling.spellcheck_hints(args, packages)
        else:
            logging.error("spell-checking support not available")

    # validate the package set
    if not package.validate_packages(args, packages):
        logging.error("package set has errors, not writing setup.ini")
        return

    # write setup.ini
    package.write_setup_ini(args, packages, args.arch)

    if args.stats:
        stats(packages)


#
#
#

def stats(packages):
    # make a histogram of categories
    histogram = {}

    for c in hint.categories:
        histogram[c.lower()] = 0

    for p in packages.values():
        if 'category' in p.hints:
            for c in p.hints['category'].split():
                histogram.setdefault(c.lower(), 0)
                histogram[c.lower()] += 1

    for c in sorted(histogram, key=histogram.get, reverse=True):
        print('%16s: %4d' % (c, histogram[c]))


#
#
#
def main():
    pkglist_default = common_constants.PKGMAINT
    relarea_default = common_constants.FTP

    parser = argparse.ArgumentParser(description='Make setup.ini')
    parser.add_argument('--arch', action='store', required=True, choices=common_constants.ARCHES)
    parser.add_argument('--inifile', '-u', action='store', help='output filename', required=True)
    parser.add_argument('--okmissing', action='append', help='missing things which are ok', choices=['curr', 'required-package'])
    parser.add_argument('--pkglist', action='store', nargs='?', metavar='FILE', help="package maintainer list (default: " + pkglist_default + ")", const=pkglist_default)
    parser.add_argument('--release', action='store', help='value for setup-release key (default: cygwin)', default='cygwin')
    parser.add_argument('--releasearea', action='store', metavar='DIR', help="release directory (default: " + relarea_default + ")", default=relarea_default, dest='rel_area')
    parser.add_argument('--spell', action='store_true', help='spellcheck text hints')
    parser.add_argument('--stats', action='store_true', help='show additional package statistics')
    parser.add_argument('--setup-version', action='store', metavar='VERSION', help='value for setup-version key')
    parser.add_argument('-v', '--verbose', action='count', dest='verbose', help='verbose output')
    (args) = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    logging.basicConfig(format=os.path.basename(sys.argv[0])+': %(message)s')

    do_main(args)


#
#
#

if __name__ == "__main__":
    sys.exit(main())
