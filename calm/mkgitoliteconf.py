#!/usr/bin/env python3
#
# Copyright (c) 2017 Jon Turney
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
# mkgitoliteconf - creates a gitolite conf file fragment from cygwin-pkg-maint
#

from collections import defaultdict
import argparse
import sys

from . import common_constants
from . import maintainers


#
# transform username to charset acceptable to gitolite
#


def transform_username(name):
    name = name.replace('.', '')
    name = name.replace(' ', '_')
    return name


#
#
#

def do_main(args):
    # read maintainer list
    mlist = {}
    mlist = maintainers.Maintainer.add_packages(mlist, args.pkglist, getattr(args, 'orphanmaint', None))

    # make the list of all packages
    maintainers.Maintainer.all_packages(mlist)

    # invert to a per-package list of maintainers
    pkgs = defaultdict(list)
    # for each maintainer
    for m in mlist.values():
        # for each package
        for p in m.pkgs:
            # add the maintainer name
            pkgs[p].append(m.name)

    # header
    print("# automatically generated by mkgitoliteconf")

    # for each package
    for p in sorted(pkgs):
        users = ' '.join(map(transform_username, pkgs[p]))
        owner = pkgs[p][0]  # first named maintainer
        if p.startswith('_'):
            p = p[1:]

        print("repo git/cygwin-packages/%s" % (p))
        print("C  = %s" % (users))
        print("RW = %s" % (users))
        print("owner = %s" % (owner))
        print("")


#
#
#

def main():
    pkglist_default = common_constants.PKGMAINT

    parser = argparse.ArgumentParser(description='gitolite rules config generator')
    parser.add_argument('--pkglist', action='store', metavar='FILE', help="package maintainer list (default: " + pkglist_default + ")", default=pkglist_default)
    (args) = parser.parse_args()

    do_main(args)

    return 0


#
#
#

if __name__ == "__main__":
    sys.exit(main())
