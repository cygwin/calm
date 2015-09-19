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
# project constants
#

# XXX: make these settable via command line options, with defaults?

# base directory for maintainer upload directories
HOMEDIRS='/sourceware/cygwin-staging/home'

# the 'release area', contains all released files, which are rsync'ed to mirrors
#FTP='/var/ftp/pub/cygwin'
FTP='/var/ftp/pub/cygwin-test'

# logs are always emailed to these addresses
#EMAIL='jturney'
EMAILS='jon.turney@dronecode.org.uk'

# these maintainers can upload orphaned packages as well
ORPHANMAINT="Yaakov Selkowitz"

# architectures we support
ARCHES=['x86', 'x86_64' ]


# base directory for HTML output
HTDOCS='/www/sourceware/htdocs/cygwin/'

#
#HTMLBASE='/www/sourceware/htdocs/cygwin/packages'

# the list of packages with maintainers
PKGMAINT='/www/sourceware/htdocs/cygwin/cygwin-pkg-maint'