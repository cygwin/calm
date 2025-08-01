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
# utilities for working with a package database
#

import copy
import difflib
import hashlib
import json
import logging
import os
import pprint
import re
import textwrap
import time
from collections import defaultdict
from enum import Enum, IntEnum, unique

import xtarfile

from . import common_constants
from . import hint
from . import maintainers
from . import past_mistakes
from . import utils
from .movelist import MoveList
from .version import SetupVersion


# kinds of packages
@unique
class Kind(Enum):
    binary = 1  # aka 'install'
    source = 2


@unique
class Importance(IntEnum):
    base = 1     # has base category
    basedep = 2  # doesn't have base category, but is depended on by something that does
    other = 3    # all others

    def __str__(self):
        return self.name


# a path inside a package repository (e.g relative to relarea)
class RepoPath():
    def __init__(self, _arch=None, _path=None, _fn=None):
        self.arch = _arch
        self.path = _path
        self.fn = _fn

    def __eq__(self, other):
        return (self.arch == other.arch and
                self.path == other.path and
                self.fn == other.fn)

    # convert to a path, absolute if given a base directory
    def abspath(self, basedir=None):
        pc = [self.arch, 'release', self.path, self.fn]
        if basedir:
            pc.insert(0, basedir)
        return os.path.join(*pc)

    # convert to a MoveList tuple
    def move(self):
        return (os.path.join(self.arch, 'release', self.path), self.fn)

    def __repr__(self):
        return "RepoPath(%s, %s, %s)" % (self.arch, self.path, self.fn)


# information we keep about a package
class Package(object):
    def __init__(self):
        self.tarfiles = {}
        self.hints = {}
        self.is_used_by = set()
        self.version_hints = {}
        self.override_hints = {}
        self.not_for_output = False
        self.auth_path = set()

    def __repr__(self):
        return "Package('%s', %s, %s, %s, %s)" % (
            self.name,
            pprint.pformat(self.tarfiles),
            pprint.pformat(self.version_hints),
            pprint.pformat(self.override_hints),
            self.not_for_output)

    def tar(self, vr):
        return self.tarfiles[vr]

    def versions(self):
        return self.tarfiles.keys()

    def srcpackage(self, vr, suffix=True):
        if self.kind == Kind.source:
            spn = self.name
        else:
            # source tarfile is in the external-source package, if specified,
            # otherwise it's in the sibling source package
            hints = self.version_hints.get(vr, {})
            spn = hints.get('external-source', self.name + '-src')

        # strip '-src' suffix?
        if not suffix:
            if spn.endswith('-src'):
                spn = spn[:-4]

        return spn


# information we keep about a tar file
class Tar(object):
    def __init__(self):
        self.repopath = RepoPath()  # pathname of tar archive
        self.sha512 = ''
        self.size = 0
        self.is_empty = False
        self.is_used = False

    def __repr__(self):
        return "Tar('%s', '%s', '%s', %d, %s)" % (self.repopath.fn, os.path.join(self.repopath.arch, 'release', self.repopath.path), self.sha512, self.size, self.is_empty)


# information we keep about a hint file
class Hint(object):
    def __init__(self):
        self.repopath = RepoPath()  # pathname of hint file
        self.hints = {}   # XXX: duplicates version_hints, for the moment


#
# read a packages from a directory hierarchy
#
def read_packages(rel_area):
    result = False

    # first collect all package files
    # all <arch>/, noarch/ and src/ directories are considered
    collected = {}
    for root in ['noarch', 'src'] + common_constants.ARCHES:
        releasedir = os.path.join(rel_area, root)

        for (dirpath, _subdirs, files) in os.walk(releasedir, followlinks=True):
            result = collect_files_package_dir(collected, rel_area, dirpath, files) or result

    # then read each package
    logging.debug('reading packages from %s' % rel_area)

    packages = {}
    for p in collected:
        fl = collected[p]
        for kind in Kind:
            if not fl[kind]:
                continue

            result = read_one_package(packages, p, rel_area, fl[kind] + fl['all'], kind, strict=False) or result

    logging.debug("%d packages read from %s" % (len(packages), rel_area))

    return (packages, result)


# helper function to compute sha512 for a particular file
# (block_size should be some multiple of sha512 block size which can be efficiently read)
def sha512_file_hash(fn, block_size=256 * 128):
    sha512 = hashlib.sha512()

    with open(fn, 'rb') as f:
        for chunk in iter(lambda: f.read(block_size), b''):
            sha512.update(chunk)

    return sha512.hexdigest()


# helper function to parse a sha512.sum file
@utils.mtime_cache
def sha512sum_file_read(sum_fn):
    sha512 = {}
    with open(sum_fn) as fo:
        for l in fo:
            match = re.match(r'^(\S+)\s+(?:\*|)(\S+)$', l)
            if match:
                sha512[match.group(2)] = match.group(1)
            else:
                logging.warning("bad line '%s' in checksum file %s" % (l.strip(), sum_fn))

    return sha512


# helper function to determine sha512 for a particular file
#
# read sha512 checksum from a sha512.sum file, if present, otherwise compute it
def sha512_file(fn):
    (dirname, basename) = os.path.split(fn)
    sum_fn = os.path.join(dirname, 'sha512.sum')
    if os.path.exists(sum_fn):
        sha512 = sha512sum_file_read(sum_fn)
        if basename in sha512:
            return sha512[basename]
        else:
            logging.debug("no line for file %s in checksum file %s" % (basename, sum_fn))
    else:
        logging.debug("no sha512.sum in %s" % dirname)

    sha512 = sha512_file_hash(fn)
    logging.debug("computed sha512 hash for %s is %s" % (basename, sha512))
    return sha512


# process a list of package version-constraints
def process_package_constraint_list(pcl):
    # split, keeping optional version-relation, trim and sort
    deplist = {}

    if ',' in pcl:
        # already comma separated is simple
        for r in pcl.split(','):
            r = r.strip()
            item = re.sub(r'(.*)\s+\(.*?\)', r'\1', r)
            deplist[item] = r
    else:
        # otherwise, split into a sequence of package names, version-relation
        # constraints and white-space, and group package name with any following
        # constraint
        item = None
        for r in re.split(r'(\(.*?\)|\s+)', pcl):
            r = r.strip()
            if not r:
                continue
            if r.startswith('('):
                if not item:
                    logging.warning("constraint '%s' before any package" % (r))
                elif '(' in deplist[item]:
                    logging.warning("multiple constraints after package %s" % (item))
                else:
                    deplist[item] = item + ' ' + r
            else:
                item = r
                deplist[item] = past_mistakes.substitute_dependency.get(r, r)

    # return a sorted list of package names with an optional version constraint.
    return sorted(deplist.values())


# helper function to read hints
def read_hints(p, fn, kind, strict=False):
    hints = hint.hint_file_parse(fn, kind, strict)

    if 'parse-errors' in hints:
        for l in hints['parse-errors']:
            logging.error("package '%s': %s" % (p, l))
        logging.error("errors while parsing hints for package '%s'" % p)
        return None

    if 'parse-warnings' in hints:
        for l in hints['parse-warnings']:
            logging.info("package '%s': %s" % (p, l))

    # convert hint keys which have a value which is a list to an actual list (to
    # avoid doing the splitting and whitespace handling everywhere)
    #
    # XXX: guarantee they exist and are an empty list if empty, so we don't need
    # to check if they exist everywhere?)
    for k in ['obsoletes', 'provides', 'conflicts', 'build-depends']:
        if k in hints:
            v = hints[k].strip()
            if not v:
                v = []
            else:
                # split on comma, remove any extraneous whitespace
                v = [i.strip() for i in v.split(',')]
            hints[k] = v

    # 'depends' is special, generated from from requires:
    hints['depends'] = process_package_constraint_list(hints.get('requires', ''))
    # erase requires:, to ensure there is nothing still using it
    hints.pop('requires', None)

    # disable check is just whitespace separated
    if 'disable-check' in hints:
        hints['disable-check'] = hints['disable-check'].split()

    return hints


# helper function to clean up hints
def clean_hints(p, hints, warnings):
    #
    # fix some common defects in the hints
    #

    # don't allow a redundant 'package:' or 'package - ' at start of sdesc
    #
    # match case-insensitively, and use a base package name (trim off any
    # leading 'lib' from package name, remove any soversion or 'devel'
    # suffix)
    #
    if 'sdesc' in hints:
        colon = re.match(r'^"(.*?)(\s*:|\s+-)', hints['sdesc'])
        if colon:
            package_basename = re.sub(r'^lib(.*?)(|-devel|\d*)$', r'\1', p)
            if package_basename.upper().startswith(colon.group(1).upper()):
                logging.error("package '%s' sdesc starts with '%s'; this is redundant as the UI will show both the package name and sdesc" % (p, ''.join(colon.group(1, 2))))
                warnings = True

    return warnings


#
# collect package files from a single package directory
#
# (may contain at most one source package and one binary package)
# (updates collected, and returns True if problems, False otherwise)
#

def collect_files_package_dir(collected, basedir, dirpath, files):
    relpath = os.path.relpath(dirpath, basedir)

    # skip over <arch>/release/ directories
    if relpath.count(os.sep) < 2:
        return

    (arch, release, pkgpath) = relpath.split(os.sep, 2)
    assert release == 'release'

    # the package name is always the directory name
    p = os.path.basename(dirpath)

    # ignore dotfiles, backup files and checksum files
    for f in files[:]:
        if f.startswith('.') or f.endswith('.bak') or f == 'sha512.sum':
            files.remove(f)

    # no .hint files
    if not any([f.endswith('.hint') for f in files]):
        if (relpath.count(os.path.sep) > 1):
            if len(files) > 0:
                logging.error("no .hint files in %s but has files: %s" % (dirpath, ', '.join(files)))
                return True

        return False

    # initialize package in collected
    if p not in collected:
        collected[p] = {}
        for kind in list(Kind) + ['all']:
            collected[p][kind] = []

    fl = collected[p]

    # classify files for which kind of package they belong to
    for f in files[:]:
        if f == 'override.hint':
            fl['all'].append(RepoPath(arch, pkgpath, f))
            files.remove(f)
        elif re.match(r'^' + re.escape(p) + r'.*\.hint$', f):
            if f.endswith('-src.hint'):
                fl[Kind.source].append(RepoPath(arch, pkgpath, f))
            else:
                fl[Kind.binary].append(RepoPath(arch, pkgpath, f))
            files.remove(f)
        elif re.match(r'^' + re.escape(p) + r'.*\.tar' + common_constants.PACKAGE_COMPRESSIONS_RE + r'$', f):
            if '-src.tar' in f:
                fl[Kind.source].append(RepoPath(arch, pkgpath, f))
            else:
                fl[Kind.binary].append(RepoPath(arch, pkgpath, f))
            files.remove(f)

    # warn about unexpected files, including tarfiles which don't match the
    # package name
    if files:
        logging.error("unexpected files in %s: %s" % (p, ', '.join(files)))
        return True

    return False


#
# read a single package
#
def read_one_package(packages, p, basedir, files, kind, strict):
    warnings = False

    if not re.match(r'^[\w\-._+]*$', p):
        logging.error("package '%s' name contains illegal characters" % p)
        return True

    if re.search(r'-\d', p):
        logging.error("package '%s' name contains hyphen followed a digit" % p)
        return True

    # assumption: no real package names end with '-src'
    #
    # enforce this, because source and install package names exist in a
    # single namespace currently, and otherwise there could be a collision
    if p.endswith('-src'):
        logging.error("package '%s' name ends with '-src'" % p)
        return True

    pn = p + ('-src' if kind == Kind.source else '')

    if pn in packages:
        logging.error("duplicate package name %s" % (pn))
        return True

    # collecting multiple copies of any filename is an error
    # (we could easily do this by accident with override.hint)
    duplicates = set()
    seen = set()
    for f in files:
        if f.fn in seen:
            duplicates.add(f.fn)
        seen.add(f.fn)

    if duplicates:
        for d in duplicates:
            logging.error("Multiple %s files for package '%s'.  I can't handle that!" % (d, p))
        return True

    # read any override.hint
    override_hints = {}
    for rp in list(files):
        if rp.fn == 'override.hint':
            override_hints = read_hints(p, rp.abspath(basedir), hint.override)
            if override_hints is None:
                logging.error("error parsing %s" % (rp.fn))
                return True
            files.remove(rp)
            break

    # build a list of version-releases (since replacement pvr.hint files are
    # allowed to be uploaded, we must consider both .tar and .hint files for
    # that), and collect the attributes for each tar file
    tars = {}
    hint_files = {}
    vr_list = set()
    auth_path = set()

    for rp in list(files):
        f = rp.fn

        if kind == Kind.source:
            arch_re = r'(-src)'
        else:
            # archtag is either missing, or the appropriate one for the path
            arch_re = r'(-' + rp.arch + r'|)'

        # warn if filename doesn't follow P-V-R naming convention
        #
        # P must match the package name, V can contain anything, R must
        # start with a number and can't include a hyphen
        match = re.match(r'^' + re.escape(p) + r'-(.+)-(\d[0-9a-zA-Z._+]*)' + arch_re + r'\.(tar' + common_constants.PACKAGE_COMPRESSIONS_RE + r'|hint)$', f)
        if not match:
            logging.error("file '%s' in package '%s' doesn't follow naming convention" % (rp.fn, p))
            return True
        else:
            v = match.group(1)
            r = match.group(2)

            # historically, V can contain a '-' (since we can use the fact
            # we already know P to split unambiguously), but this is a bad
            # idea.
            if '-' in v:
                if v in past_mistakes.illegal_char_in_version.get(p, []):
                    lvl = logging.INFO
                else:
                    lvl = logging.ERROR
                    warnings = True
                logging.log(lvl, "file '%s' in package '%s' contains '-' in version" % (f, p))

            if not v[0].isdigit():
                logging.error("file '%s' in package '%s' has a version which doesn't start with a digit" % (f, p))
                warnings = True

            if not re.match(r'^[\w\-._+]*$', v):
                if v in past_mistakes.illegal_char_in_version.get(p, []):
                    lvl = logging.INFO
                else:
                    lvl = logging.ERROR
                    warnings = True
                logging.log(lvl, "file '%s' in package '%s' has a version which contains illegal characters" % (f, p))

            # if not there already, add to version-release list
            vr = '%s-%s' % (v, r)
            vr_list.add(vr)

        if not f.endswith('.hint'):
            # a package can only contain tar archives of the appropriate type
            if not re.search(r'-src.*\.tar', f):
                if kind == Kind.source:
                    logging.error("source package '%s' has install archives" % (p))
                    return True
            else:
                if kind == Kind.binary:
                    logging.error("package '%s' has source archives" % (p))
                    return True

            # for each version, a package can contain at most one tar file (of
            # the appropriate type). Warn if we have too many (for e.g. both a
            # .xz and .bz2 install tar file).
            if vr in tars:
                logging.error("package '%s' has more than one tar file for version '%s'" % (p, vr))
                return True

            # collect the attributes for each tar file
            t = Tar()
            t.repopath = rp
            t.size = os.path.getsize(rp.abspath(basedir))
            t.is_empty = tarfile_is_empty(rp.abspath(basedir))
            t.mtime = os.path.getmtime(rp.abspath(basedir))
            t.sha512 = sha512_file(rp.abspath(basedir))

            # record the arch_tag (or what it would have been, if not omitted)
            if kind == Kind.source:
                t.arch = 'src'
            else:
                t.arch = rp.arch

            tars[vr] = t

            # it's an error to not have a corresponding pvr.hint in the same directory
            hint_fn = '%s-%s%s.hint' % (p, vr, match.group(3))
            hrp = RepoPath(rp.arch, rp.path, hint_fn)
            if hrp not in files:
                logging.error("package %s has packages for version %s, but no %s" % (p, vr, hint_fn))
                return True

        else:
            # for each version, a package can contain at most one hint
            # file. Warn if we have too many (for e.g. both with and without an
            # archtag).
            if vr in hint_files:
                logging.error("package '%s' has more than one hint file for version '%s'" % (p, vr))
                return True
            hint_files[vr] = rp

        # collect the superpackage names for authorization purposes
        auth_path.add(rp.path.split(os.sep, 1)[0])

    # determine hints for each version we've encountered
    version_hints = {}
    hints = {}
    actual_tars = {}
    for vr in vr_list:
        rp = hint_files[vr]

        # is there a PVR.hint file?
        pvr_hint = read_hints(p, rp.abspath(basedir), hint.pvr if kind == Kind.binary else hint.spvr, strict)
        if not pvr_hint:
            logging.error("error parsing %s" % (rp.fn))
            return True
        warnings = clean_hints(p, pvr_hint, warnings)

        # apply a version override
        if 'version' in pvr_hint:
            ovr = pvr_hint['version']
            # also record the version before the override
            pvr_hint['original-version'] = vr
        else:
            ovr = vr

        # external source will always point to a source package
        if 'external-source' in pvr_hint:
            pvr_hint['external-source'] += '-src'

        hintobj = Hint()
        hintobj.repopath = rp
        hintobj.hints = pvr_hint
        hintobj.mtime = os.path.getmtime(rp.abspath(basedir))

        version_hints[ovr] = pvr_hint
        hints[ovr] = hintobj
        if vr in tars:
            actual_tars[ovr] = tars[vr]

    packages[pn] = Package()
    packages[pn].name = pn
    packages[pn].version_hints = version_hints
    packages[pn].override_hints = override_hints
    packages[pn].tarfiles = actual_tars
    packages[pn].hints = hints
    packages[pn].auth_path = auth_path
    packages[pn].kind = kind
    # since we are kind of inventing the source package names, and don't
    # want to report them, keep track of the real name
    packages[pn].orig_name = p

    return warnings


#
# utility to determine if a tar file is empty
#
def tarfile_is_empty(tf):
    size = os.path.getsize(tf)

    # report invalid files (smaller than the smallest possible compressed file
    # for any of the compressions we support)
    if size < 14:
        logging.error("tar archive %s is too small (%d bytes)" % (tf, size))
        return True

    # sometimes compressed empty files are used rather than a compressed empty
    # tar archive
    if size <= 32:
        return True

    # parsing the tar archive just to determine if it contains at least one
    # archive member is relatively expensive, so we just assume it contains
    # something if it's over a certain size threshold
    if size > 1024:
        return False

    # if it's really a tar file, does it contain zero files?
    try:
        with xtarfile.open(tf, mode='r') as a:
            if any(a) == 0:
                return True
    except Exception as e:
        logging.error("exception %s while reading %s" % (type(e).__name__, tf))
        logging.debug('', exc_info=True)

    return False


# a sorting which forces packages which begin with '!' to be sorted first,
# packages which begin with '_" to be sorted last, and others to be sorted
# case-insensitively
def sort_key(k):
    k = k.lower()
    if k[0] == '!':
        k = chr(0) + k
    elif k[0] == '_':
        k = chr(255) + k
    return k


#
# generate missing_obsolete data for upgrading old-style obsoletion install
# packages:
#
# they are empty, have the '_obsolete' category, and requires: exactly 1
# package, their replacement.
#
# generate a record to add an obsoletes: header to the replacement package.
#
def upgrade_oldstyle_obsoletes(packages, missing_obsolete):
    for p in sorted(packages):
        if packages[p].kind == Kind.binary:
            for vr in sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True):
                # we only really want to consider packages where the current
                # version is obsolete.
                #
                # (if older versions are obsolete and we were somehow
                # un-obsoleted, we'd need to somehow infer version constraints
                # on the obsoletions, or something)
                #
                # as a proxy for that, stop considering versions of this package
                # when one isn't obsolete, don't consider older versions
                if not (packages[p].tar(vr).is_empty and
                        '_obsolete' in packages[p].version_hints[vr]['category']):
                    break

                requires = packages[p].version_hints[vr].get('depends', [])
                requires = deplist_without_versions(requires)

                o = None
                for oso_re, oso_o in past_mistakes.old_style_obsolete_by.items():
                    if re.match(r'^' + oso_re + r'$', p):
                        o = oso_o
                        break

                if o is not None:
                    # empty replacement means "ignore"
                    if not o:
                        continue

                    logging.debug('%s is hardcoded as obsoleted by %s ' % (p, o))

                else:
                    # ignore self-destruct packages
                    provides = packages[p].version_hints[vr].get('provides', [])
                    if '_self-destruct' in provides:
                        continue

                    if len(requires) == 0:
                        # obsolete but has no replacement
                        logging.warning('%s is obsolete, but has no replacement' % (p))
                        continue
                    elif len(requires) == 1:
                        o = requires[0]
                    elif len(requires) >= 2:
                        # obsolete with multiple replacements (pick one?)
                        logging.warning('%s %s is obsoleted by %d packages (%s)' % (p, vr, len(requires), requires))
                        continue

                # ignore if o it's blacklisted
                if o in ['cygwin-debuginfo', 'calligra-libs']:
                    logging.debug("not adding 'obsoletes: %s' to '%s' as blacklisted" % (p, o))
                    continue

                if o in packages:
                    if o not in missing_obsolete:
                        missing_obsolete[o] = set()

                    missing_obsolete[o].add(p)
                    logging.info("converting from empty, _obsolete category package '%s' to 'obsoletes: %s' in package '%s'" % (p, p, o))

    return missing_obsolete


#
# drop version constraints from a list of dependencies
#
def deplist_without_versions(dpl):
    return [re.sub(r'(.*)\s+\(.*\)', r'\1', dp) for dp in dpl]


#
# validate the package database
#
def validate_packages(args, packages, valid_provides_extra=None, missing_obsolete_extra=None):
    error = False

    if packages is None:
        logging.error("package set is empty")
        return False

    if missing_obsolete_extra is None:
        missing_obsolete_extra = {}

    # build the set of valid things to depends: on
    valid_requires = set()

    for p in packages:
        valid_requires.add(p)
        for hints in packages[p].version_hints.values():
            valid_requires.update(hints.get('obsoletes', []))
            valid_requires.update(hints.get('provides', []))

            # reset computed package state
            packages[p].has_requires = False
            packages[p].obsolete = False
            packages[p].rdepends = set()
            packages[p].build_rdepends = set()
            packages[p].obsoleted_by = set()
            packages[p].orphaned = False

    # it's also valid to requires: packages which are named in a synthetic
    # obsoletes:
    for r in missing_obsolete_extra.values():
        valid_requires.update(r)

    # it's also valid to obsoletes: packages which have been removed
    valid_obsoletes = set(valid_requires)
    if valid_provides_extra:
        valid_obsoletes.update(valid_provides_extra)

    # perform various package validations
    for p in sorted(packages.keys()):
        for (v, hints) in packages[p].version_hints.items():
            for (c, okmissing, valid) in [
                    ('depends', 'missing-depended-package', valid_requires),
                    ('obsoletes', 'missing-obsoleted-package', valid_obsoletes),
                    ('build-depends', 'missing-build-depended-package', valid_requires),
            ]:
                # if c is in hints, and not the empty string
                if hints.get(c, ''):
                    for r in deplist_without_versions(hints[c]):
                        if c == 'depends':
                            # don't count cygwin-debuginfo for the purpose of
                            # checking if this package has any requires, as
                            # cygport always makes debuginfo packages require
                            # that, even if they are empty
                            if r != 'cygwin-debuginfo':
                                packages[p].has_requires = True

                        # a package should not appear in it's own hint
                        if r == p:
                            lvl = logging.WARNING if p not in past_mistakes.self_requires else logging.DEBUG
                            logging.log(lvl, "package '%s' version '%s' %s itself" % (p, v, c))

                        # all packages listed in a hint must exist (unless the
                        # disable-check option says that's ok)
                        if (r not in valid) and (r not in past_mistakes.expired_provides) and (not any(re.match(nep + r'$', r) for nep in past_mistakes.nonexistent_provides)):
                            if okmissing not in getattr(args, 'disable_check', []):
                                logging.error("package '%s' version '%s' %s: '%s', but nothing satisfies that" % (p, v, c, r))
                                error = True
                            continue

                        # package relation hint referencing a source package makes no sense
                        if r in packages and packages[r].kind == Kind.source:
                            logging.error("package '%s' version '%s' %s source package '%s'" % (p, v, c, r))
                            error = True

            # if external-source is used, the package must exist
            if 'external-source' in hints:
                e = hints['external-source']
                if e not in packages:
                    logging.error("package '%s' version '%s' refers to non-existent or errored external-source '%s'" % (p, v, e))
                    error = True

            # some old packages are missing needed obsoletes:, add them where
            # needed, and make sure the uploader is warned if/when package is
            # updated
    for mo in [past_mistakes.missing_obsolete, missing_obsolete_extra]:
        for p in mo:
            if p in packages:
                for v in packages[p].version_hints:

                    obsoletes = packages[p].version_hints[v].get('obsoletes', [])

                    def add_needed_obsoletes(needed):
                        for n in sorted(needed):
                            if n not in obsoletes:
                                obsoletes.append(n)
                                packages[p].version_hints[v]['obsoletes'] = obsoletes
                                logging.info("added 'obsoletes: %s' to package '%s' version '%s'" % (n, p, v))
                                logging.info("this should be in fixed in the cygport packaging")

                            # recurse so we don't drop transitive missing obsoletes
                            if n in mo:
                                logging.debug("recursing to examine obsoletions of '%s' for adding to '%s'" % (n, p))
                                add_needed_obsoletes(mo[n])

                    add_needed_obsoletes(mo[p])

        # If package A is obsoleted by package B, B should appear in the
        # requires: for A (so the upgrade occurs with pre-depends: aware
        # versions of setup), but not in the depends: for A (as that creates an
        # unsatisfiable dependency when explicitly installing A with lisolv
        # versions of setup, which should just install B).  This condition can
        # occur since we might have synthesized the depends: from the requires:
        # in read_hints(), so fix that up here.
    for p in sorted(packages):
        for hints in packages[p].version_hints.values():
            obsoletes = hints.get('obsoletes', [])
            if obsoletes:
                for o in deplist_without_versions(obsoletes):
                    if o in packages:
                        packages[o].obsolete = True

                        for (ov, ohints) in packages[o].version_hints.items():
                            if 'depends' in ohints:
                                depends = ohints['depends']
                                if p in depends:
                                    depends = [d for d in depends if d != p]
                                    packages[o].version_hints[ov]['depends'] = depends
                                    logging.debug("removed obsoleting '%s' from the depends: of package '%s'" % (p, o))
                    else:
                        logging.debug("can't ensure package '%s' doesn't depends: on obsoleting '%s'" % (o, p))

        has_nonempty_install = False

        if packages[p].kind == Kind.binary:
            for vr in packages[p].versions():
                if not packages[p].tar(vr).is_empty:
                    has_nonempty_install = True

        obsolete = any(['_obsolete' in packages[p].version_hints[vr].get('category', '') for vr in packages[p].version_hints])

        # if the package has no non-empty install tarfiles, and no dependencies
        # installing it will do nothing (and making it appear in the package
        # list is just confusing), so if it's not obsolete, mark it as
        # 'not_for_output'
        if packages[p].kind == Kind.binary:
            if not has_nonempty_install and not packages[p].has_requires and not obsolete:
                if not packages[p].not_for_output:
                    packages[p].not_for_output = True
                    logging.info("package '%s' has no non-empty install tarfiles and no dependencies, marking as 'not for output'" % (p))
            else:
                packages[p].not_for_output = False

        # identify a 'best' version to take certain information from: this is
        # the curr version, if we have one, otherwise, the highest version.
        for v in sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True):
            if 'test' not in packages[p].version_hints[v]:
                packages[p].best_version = v
                break
        else:
            if len(packages[p].versions()):
                packages[p].best_version = sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True)[0]
            else:
                # the package must have some versions
                logging.error("package '%s' doesn't have any versions" % (p))
                packages[p].best_version = None
                error = True

        # error if the curr: version isn't the most recent non-test: version
        mtimes = {}
        for vr in packages[p].versions():
            mtimes[vr] = packages[p].tar(vr).mtime

        cv = None
        nontest_versions = [v for v in sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True) if 'test' not in packages[p].version_hints.get(v, {})]
        if len(nontest_versions) >= 1:
            cv = nontest_versions[0]

        for v in sorted(packages[p].versions(), key=lambda v: mtimes[v], reverse=True):
            if 'test' in packages[p].version_hints[v]:
                continue

            if cv not in packages[p].versions():
                continue

            if cv != v:
                if mtimes[v] == mtimes[cv]:
                    # don't consider an equal mtime to be more recent
                    continue

                if ((p in past_mistakes.mtime_anomalies) or
                    ('curr-most-recent' in packages[p].override_hints.get('disable-check', '')) or
                    ('curr-most-recent' in getattr(args, 'disable_check', []))):
                    lvl = logging.DEBUG
                else:
                    lvl = logging.ERROR
                    error = True
                logging.log(lvl, "package '%s' ordering discrepancy in non-test versions: '%s' has most recent timestamp, but version '%s' is greatest" % (p, v, cv))
            break

        if 'replace-versions' in packages[p].override_hints:
            for rv in packages[p].override_hints['replace-versions'].split():
                # warn if replace-versions lists a version which is less than
                # the current version (which is pointless as the current version
                # will replace it anyhow)
                bv = packages[p].best_version
                if bv:
                    if SetupVersion(rv) <= SetupVersion(bv):
                        logging.warning("package '%s' replace-versions: uselessly lists version '%s', which is <= current version '%s'" % (p, rv, bv))

                # warn if replace-versions lists a version which is also
                # available to install (as this doesn't work as expected)
                if rv in packages[p].version_hints:
                    if ('test' in packages[p].version_hints[rv]) == ('test' in packages[p].version_hints[bv]):
                        logging.warning("package '%s' replace-versions: lists version '%s', which is also available to install" % (p, rv))

        # If the install tarball is empty, we should probably either be marked
        # obsolete (if we have no dependencies) or virtual (if we do)
        if packages[p].kind == Kind.binary and not packages[p].not_for_output:
            for vr in packages[p].versions():
                if packages[p].tar(vr).is_empty:
                    # this classification relies on obsoleting packages
                    # not being present in depends
                    if packages[p].version_hints[vr].get('depends', []):
                        # also allow '_obsolete' because old obsoletion
                        # packages depend on their replacement, but are not
                        # obsoleted by it
                        expected_categories = ['virtual', '_obsolete']
                    else:
                        expected_categories = ['_obsolete']

                    if all(c not in packages[p].version_hints[vr].get('category', '').lower() for c in expected_categories):
                        if ((vr in past_mistakes.empty_but_not_obsolete.get(p, [])) or
                            ('empty-obsolete' in packages[p].version_hints[vr].get('disable-check', ''))):
                            lvl = logging.DEBUG
                        else:
                            lvl = logging.ERROR
                            error = True
                        logging.log(lvl, "package '%s' version '%s' has empty install tar file, but it's not in %s category" % (p, vr, expected_categories))
        # If the source tarball is empty, that can't be right!
        elif packages[p].kind == Kind.source:
            for vr in packages[p].versions():
                if packages[p].tar(vr).is_empty:
                    if ((vr in past_mistakes.empty_source.get(p, [])) and
                        '_obsolete' in packages[p].version_hints[vr].get('category', '')):
                        lvl = logging.DEBUG
                    else:
                        error = True
                        lvl = logging.ERROR
                    logging.log(lvl, "package '%s' version '%s' has empty source tar file" % (p, vr))

    # build inverted relations:
    # the set of packages which depends: on this package (rdepends),
    # the set of packages which build-depends: on it (build_rdepends), and
    # the set of packages which obsoletes: it (obsoleted_by)
    for p in packages:
        for hints in packages[p].version_hints.values():
            for k, a in [
                    ('depends', 'rdepends'),
                    ('build-depends', 'build_rdepends'),
                    ('obsoletes', 'obsoleted_by'),
            ]:
                if k in hints:
                    for dp in deplist_without_versions(hints[k]):
                        if dp in packages:
                            getattr(packages[dp], a).add(p)

    # warn about multiple obsoletes of same package
    for p in sorted(packages.keys()):
        if len(packages[p].obsoleted_by) >= 2:
            logging.debug("package '%s' is obsoleted by more than one package: %s" % (p, ','.join(packages[p].obsoleted_by)))

    # make another pass to verify a source tarfile exists for every install
    # tarfile version
    for p in packages.keys():
        packages[p].is_used_by = set()

    for p in sorted(packages.keys()):
        if not packages[p].kind == Kind.binary:
            continue

        for v in sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True):
            sourceless = False
            missing_source = True

            es_p = packages[p].srcpackage(v)

            # mark the source tarfile as being used by an install tarfile
            if es_p in packages:
                if v in packages[es_p].versions():
                    packages[es_p].tar(v).is_used = True
                    packages[es_p].is_used_by.add(p)
                    missing_source = False

                    # also check that they match in presence or absence test: label
                    #
                    # (this is needed if we are going to compare best_version
                    # between install and source packages for some information,
                    # as we do in some places...)
                    if ('test' in packages[p].version_hints[v]) != ('test' in packages[es_p].version_hints[v]):
                        logging.error("package '%s' version '%s' test: label mismatches source package '%s'" % (p, v, es_p))

            if missing_source:
                # unless the install tarfile is empty
                if packages[p].tar(v).is_empty:
                    sourceless = True
                    missing_source = False

                # unless this package is marked as 'self-source'
                if p in past_mistakes.self_source:
                    sourceless = True
                    missing_source = False

            # ... it's an error for this package to be missing source
            packages[p].tar(v).sourceless = sourceless
            if missing_source:
                logging.error("package '%s' version '%s' is missing source" % (p, v))
                error = True

    # make another pass to verify that each non-empty source tarfile version has
    # at least one corresponding non-empty install tarfile, in some package.
    for p in sorted(packages.keys()):
        for v in sorted(packages[p].versions(), key=lambda v: SetupVersion(v), reverse=True):
            if not packages[p].kind == Kind.source:
                continue

            if packages[p].tar(v).is_empty:
                continue

            if '_obsolete' in packages[p].version_hints[v].get('category', ''):
                continue

            if not packages[p].tar(v).is_used:
                logging.error("package '%s' version '%s' source has no non-empty install tarfiles" % (p, v))
                error = True

    # do all the packages which use this source package have the same
    # current version?
    for source_p in sorted(packages.keys()):
        versions = defaultdict(list)

        for install_p in packages[source_p].is_used_by:
            # ignore package which are getting removed
            if install_p not in packages:
                continue

            # ignore obsolete packages
            if packages[install_p].obsolete:
                continue
            if any(['_obsolete' in packages[install_p].version_hints[vr].get('category', '') for vr in packages[install_p].version_hints]):
                continue

            # ignore runtime library packages, as we may keep old versions of
            # those
            if re.match(common_constants.SOVERSION_PACKAGE_RE, install_p):
                continue

            # ignore Python module packages, as we may keep old versions of
            # those
            if re.match(r'^python3\d+-.*', install_p):
                continue

            # ignore packages where best_version is a test version (i.e doesn't
            # have a current version, is test only), since the check we are
            # doing here is 'same current version'
            if 'test' not in packages[install_p].version_hints[packages[install_p].best_version]:
                continue

            # ignore packages which have a different external-source:
            # (e.g. where a different source package supersedes this one)
            es = packages[install_p].srcpackage(packages[install_p].best_version)
            if es != source_p:
                continue

            # ignore specific packages we disable this check for
            if ((install_p in past_mistakes.nonunique_versions) or
                ('unique-version' in packages[install_p].version_hints[packages[install_p].best_version].get('disable-check', ''))):
                continue

            versions[packages[install_p].best_version].append(install_p)

        if len(versions) > 1:
            out = []
            most_common = True

            for v in sorted(versions, key=lambda v: len(versions[v]), reverse=True):
                # try to keep the output compact by not listing all the packages
                # the most common current version has, unless it's only one.
                if most_common and len(versions[v]) != 1:
                    out.append("%s (%s others)" % (v, len(versions[v])))
                else:
                    out.append("%s (%s)" % (v, ','.join(versions[v])))
                most_common = False

            error = True
            logging.error("install packages from source package '%s' have non-unique current versions %s" % (packages[source_p].orig_name, ', '.join(reversed(out))))

    # validate that all packages are in the package maintainers list
    error = validate_package_maintainers(args, packages) or error

    assign_importance(packages)

    return not error


# assign importance classes to packages
def assign_importance(packages):
    # XXX: if we had some package popularity data, we'd use it here
    for po in packages.values():
        po.importance = Importance.other

    # recursively give dependencies of base packages the basedep importance
    def recursive_basedep(p):
        bv = p.best_version
        requires = p.version_hints[bv].get('depends', [])
        requires = deplist_without_versions(requires)
        for r in requires:
            if r in packages:
                if packages[r].importance == Importance.other:
                    packages[r].importance = Importance.basedep
                    recursive_basedep(packages[r])

    for po in packages.values():
        bv = po.best_version
        categories = po.version_hints[bv]['category'].lower().split()
        if 'base' in categories:
            recursive_basedep(po)

    # base packages have base importance
    for po in packages.values():
        bv = po.best_version
        categories = po.version_hints[bv]['category'].lower().split()
        if 'base' in categories:
            po.importance = Importance.base

    # a source package has the importance of it's most important install package
    for po in packages.values():
        if po.kind == Kind.source:
            for ip in po.is_used_by:
                po.importance = min(po.importance, packages[ip].importance)


#
def validate_package_maintainers(args, packages):
    error = False
    if not args.pkglist:
        return error

    # read package maintainer list
    pkg_maintainers = maintainers.pkg_list(args.pkglist)

    # make the list of all packages
    all_packages = pkg_maintainers.keys()

    # validate that all packages are in the package list
    for p in sorted(packages):
        # ignore obsolete packages
        if packages[p].obsolete:
            continue
        if any(['_obsolete' in packages[p].version_hints[vr].get('category', '') for vr in packages[p].version_hints]):
            continue
        # validate that the package's auth_paths are all in the package list
        for a in packages[p].auth_path:
            if a not in all_packages:
                logging.error("package '%s' exists in '%s', which isn't in the package list" % (p, a))
                error = True
        # source which is superseded by a different package, but retained due to
        # old install versions can be unmaintained and non-obsolete
        if packages[p].kind == Kind.source:
            continue
        # validate that the source package has a maintainer
        bv = packages[p].best_version
        if bv:
            es = packages[p].srcpackage(bv)
            if es in packages:
                es_pn = packages[es].orig_name
                if es_pn not in all_packages and p not in all_packages:
                    if bv not in past_mistakes.maint_anomalies.get(p, []):
                        logging.error("package '%s' is not obsolete, but has no maintainer" % (p))
                        error = True

                if (es_pn in pkg_maintainers) and (pkg_maintainers[es_pn].is_orphaned()):
                    # note orphaned packages
                    packages[p].orphaned = True

    return error


#
# certain validation warnings we only want to issue once, when the package is
# uploaded
#
def packages_warnings(args, packages, *modified):
    for m in modified:
        for p in sorted(m):
            for v in packages[p].versions():
                if 'test' not in packages[p].version_hints[v]:
                    break
            else:
                # warn if no non-test ('curr') version exists
                if (('missing-curr' not in packages[p].version_hints[packages[p].best_version].get('disable-check', '')) and
                    ('missing-curr' not in getattr(args, 'disable_check', []))):
                    logging.warning("package '%s' doesn't have any non-test versions (i.e. no curr: version)" % (p))


#
# write setup.ini
#
def write_setup_ini(args, packages, arch):

    logging.debug('writing %s' % (args.inifile))

    with open(args.inifile, 'w') as f:
        tz = time.time()
        # write setup.ini header
        print(textwrap.dedent('''\
        # This file was automatically generated at %s.
        #
        # If you edit it, your edits will be discarded next time the file is
        # generated.
        #
        # See https://sourceware.org/cygwin-apps/setup.ini.html for a description
        # of the format.''')
              % (time.strftime("%F %T %Z", time.localtime(tz))),
              file=f)

        if args.release:
            print("release: %s" % args.release, file=f)
        print("arch: %s" % arch, file=f)
        print("setup-timestamp: %d" % tz, file=f)

        if args.setup_version:
            # this token exists in the lexer, but not in the grammar up until
            # 2.878 (when it was removed), so will cause a parse error with
            # versions prior to that.
            print("include-setup: setup <2.878 not supported", file=f)

            # not implemented until 2.890, ignored by earlier versions
            print("setup-minimum-version: 2.903", file=f)

            # for setup to check if a setup upgrade is possible
            print("setup-version: %s" % args.setup_version, file=f)

        # for each package
        for pn in sorted(packages, key=sort_key):
            po = packages[pn]

            if po.kind == Kind.source:
                continue

            # do nothing if not_for_output
            if po.not_for_output:
                continue

            # XXX: TODO for multiarch: filter version list where package exists
            # for this arch (skip this package if it ends up empty)

            # write package data
            print("\n@ %s" % pn, file=f)

            bv = po.best_version
            print("sdesc: %s" % po.version_hints[bv]['sdesc'], file=f)

            if 'ldesc' in po.version_hints[bv]:
                print("ldesc: %s" % po.version_hints[bv]['ldesc'], file=f)

            # mark orphaned packages with the 'unmaintained' pseudo-category
            category = po.version_hints[bv]['category']
            if po.orphaned:
                category += ' unmaintained'
            # for historical reasons, category names must start with a capital
            # letter
            category = ' '.join(map(upper_first_character, category.split()))
            print("category: %s" % category, file=f)

            if 'message' in po.version_hints[bv]:
                print("message: %s" % po.version_hints[bv]['message'], file=f)

            if 'replace-versions' in po.override_hints:
                print("replace-versions: %s" % po.override_hints['replace-versions'], file=f)

            # make a list of version sections
            #
            # (they are put in a particular order to ensure certain behaviour
            # from setup)
            vs = []

            # put 'curr' first
            #
            # due to a historic bug in setup (fixed in 78e4c7d7), we keep the
            # [curr] version first, to ensure that dependencies are used
            # correctly.
            nontest_versions = [v for v in sorted(po.versions(), key=lambda v: SetupVersion(v), reverse=True) if 'test' not in po.version_hints.get(v, {})]
            curr_version = None
            if len(nontest_versions) >= 1:
                curr_version = nontest_versions[0]
                vs.append((curr_version, 'curr'))

            # purely for compatibility with previous ordering, identify the
            # 'prev' version (the non-test version before the current version),
            # if it exists, so we can put it last.
            prev_version = None
            if len(nontest_versions) >= 2:
                prev_version = nontest_versions[1]

            # ditto the 'test' version
            test_version = None
            test_versions = [v for v in sorted(po.versions(), key=lambda v: SetupVersion(v), reverse=True) if 'test' in po.version_hints.get(v, {})]
            if len(test_versions) >= 1:
                test_version = test_versions[0]

            # next put any other versions
            #
            # these [prev] or [test] sections are superseded by the final ones.
            #
            # (to maintain historical behaviour, include versions which only
            # exist as a source package)
            #
            versions = set(po.versions())
            sibling_src = pn + '-src'
            if sibling_src in packages:
                versions.update(packages[sibling_src].versions())

            for version in sorted(versions, key=lambda v: SetupVersion(v), reverse=True):
                # skip over versions which have a special place in the ordering:
                # 'curr' has already been done, 'prev' and 'test' will be done
                # later
                if ((version == curr_version) or (version == prev_version) or
                    (version == test_version)):
                    continue

                # test versions receive the test label
                if 'test' in po.version_hints.get(version, {}):
                    level = "test"
                else:
                    level = "prev"
                vs.append((version, level))

            # add the 'prev' version
            if prev_version:
                vs.append((prev_version, "prev"))

            # finally, add the 'test' version
            #
            # because setup processes version sections in order, these supersede
            # any previous [prev] and [test] sections (hopefully).  i.e. the
            # version in the final [test] section is the one selected when test
            # packages are requested.
            if test_version:
                vs.append((test_version, "test"))

            # write the section for each version
            for (version, tag) in vs:
                # [curr] can be omitted if it's the first section
                if tag != 'curr':
                    print("[%s]" % tag, file=f)
                print("version: %s" % version, file=f)

                is_empty = False
                if version in po.versions():
                    tar_line(po, 'install', version, f)
                    is_empty = po.tar(version).is_empty

                hints = po.version_hints.get(version, {})

                # follow external-source
                s = po.srcpackage(version)
                if s not in packages:
                    s = None

                # external-source points to a source file in another package
                if s:
                    if version in packages[s].versions():
                        tar_line(packages[s], 'source', version, f)
                    else:
                        if not (is_empty or packages[s].orig_name in past_mistakes.self_source):
                            logging.warning("package '%s' version '%s' has no source in '%s'" % (pn, version, packages[s].orig_name))

                # external-source should also be capable of pointing to a 'real'
                # source package (if cygport could generate such a thing), in
                # which case we should emit a 'Source:' line, and the package is
                # also itself emitted.

                if version in po.versions():
                    if hints.get('depends', ''):
                        print("depends2: %s" % ', '.join(hints.get('depends', [])), file=f)

                    if hints.get('obsoletes', ''):
                        print("obsoletes: %s" % ', '.join(hints['obsoletes']), file=f)

                    if hints.get('provides', ''):
                        print("provides: %s" % ', '.join(hints['provides']), file=f)

                    if hints.get('conflicts', ''):
                        print("conflicts: %s" % ', '.join(hints['conflicts']), file=f)

                if s:
                    src_hints = packages[s].version_hints.get(version, {})
                    bd = src_hints.get('build-depends', [])

                    # Ideally, we'd transform dependency atoms which aren't
                    # cygwin package names into package names. For the moment,
                    # we don't have the information to do that, so filter them
                    # all out.
                    bd = [atom for atom in bd if '(' not in atom]

                    if bd:
                        print("build-depends: %s" % ', '.join(bd), file=f)


# helper function to output details for a particular tar file
def tar_line(p, category, v, f):
    to = p.tar(v)
    fn = to.repopath.abspath()
    sha512 = to.sha512
    size = to.size
    print("%s: %s %d %s" % (category, fn, size, sha512), file=f)


# helper function to change the first character of a string to upper case,
# without altering the rest
def upper_first_character(s):
    return s[:1].upper() + s[1:]


#
#
#

def _find_build_recipe_file(args, pn):
    if args.repodir:
        repo = os.path.join(args.repodir, '%s.git' % pn)
        if os.path.exists(repo):
            # XXX: we might want to check contents of the repo to determine if this
            # package has a cygport or g-b-s build script
            return 'https://cygwin.com/cgit/cygwin-packages/%s/tree/%s.cygport' % (pn, pn)

    return None


#
# write a json summary of packages
#
def write_repo_json(args, packages, f):
    pkg_maintainers = maintainers.pkg_list(args.pkglist)

    pl = []
    for pn in sorted(packages):
        po = packages[pn]
        arches = common_constants.ARCHES  # XXX: multiarch TODO: set of arches which have this package

        def package(p):
            if p in packages:
                return packages[p]

            # will lead to AttributeError as has no version_hints
            return None

        bv = po.best_version

        if po.kind != Kind.source:
            continue

        versions = {}
        for vr in sorted(po.version_hints.keys(), key=lambda v: SetupVersion(v)):
            key = 'test' if 'test' in po.version_hints[vr] else 'stable'
            versions[key] = versions.get(key, []) + [vr]

        d = {
            'name': po.orig_name,
            'versions': versions,
            'summary': po.version_hints[bv]['sdesc'].strip('"'),
            'arches': arches,
        }

        spl = []
        for sp in sorted(po.is_used_by):
            hints = package(sp).version_hints[package(sp).best_version]
            sp = {'name': sp, 'categories': hints.get('category', '').split()}
            for k in ['depends', 'provides', 'obsoletes']:
                if hints.get(k, None):
                    sp[k] = hints[k]
            spl.append(sp)
        d['subpackages'] = spl

        for k in ['homepage', 'license', 'build-depends']:
            if k in po.version_hints[bv]:
                d[k] = po.version_hints[bv][k]

        build_recipe = _find_build_recipe_file(args, po.orig_name)
        if build_recipe:
            d['build_recipe'] = build_recipe

        if (po.orig_name in pkg_maintainers) and (not pkg_maintainers[po.orig_name].is_orphaned()):
            d['maintainers'] = sorted(pkg_maintainers[po.orig_name].maintainers())

        pl.append(d)

    j = {
        'repository_name': args.release,
        'timestamp': int(time.time()),
        'num_packages': len(pl),
        'packages': pl,
    }
    json.dump(j, f)


#
# merge sets of packages
#
# for each package which exist in both a and b:
# - they must exist at the same relative path
# - we combine the list of tarfiles, duplicates are not permitted
# - we use the hints from b, and warn if they are different to the hints for a
#
def merge(a, *l):
    # start with a copy of a
    c = copy.deepcopy(a)

    for b in l:
        for p in b:
            # if the package is in b but not in a, add it to the copy
            if p not in c:
                c[p] = b[p]
            # else, if the package is both in a and b, we have to do a merge
            else:
                # package must be of the same kind
                if c[p].kind != b[p].kind:
                    logging.error("package '%s' is of more than one kind" % (p))
                    return None

                if True:
                    for vr in b[p].tarfiles:
                        if vr in c[p].tarfiles:
                            logging.error("package '%s' has duplicate tarfile for version %s" % (p, vr))
                            return None
                        else:
                            c[p].tarfiles[vr] = b[p].tarfiles[vr]

                    # hints from b override hints from a, but warn if they have
                    # changed
                    for vr in b[p].version_hints:
                        c[p].version_hints[vr] = b[p].version_hints[vr]
                        if vr in c[p].version_hints:
                            if c[p].version_hints[vr] != b[p].version_hints[vr]:
                                diff = '\n'.join(difflib.ndiff(
                                    pprint.pformat(c[p].version_hints[vr]).splitlines(),
                                    pprint.pformat(b[p].version_hints[vr]).splitlines()))

                                logging.warning("package '%s' version '%s' hints changed\n%s" % (p, vr, diff))

                    # overrides from b take precedence
                    c[p].override_hints.update(b[p].override_hints)

                    # merge hint file lists
                    c[p].hints.update(b[p].hints)

                    # merge auth_path sets
                    c[p].auth_path.update(b[p].auth_path)

    return c


#
# delete a file from a package set
#

def delete(packages, path, fn):
    logging.debug("removing: %s/%s" % (path, fn))

    ex_packages = []

    # a clunky way of determining the package which owns these files
    # (which still doesn't know if it's the source or binary package)
    pn = path.split(os.sep)[-1]

    for p in packages:
        if packages[p].orig_name == pn:
            for vr in packages[p].tarfiles:
                if packages[p].tarfiles[vr].repopath.fn == fn:
                    del packages[p].tarfiles[vr]
                    break

            for h in packages[p].hints:
                if packages[p].hints[h].repopath.fn == fn:
                    del packages[p].hints[h]
                    del packages[p].version_hints[h]
                    break

        # if nothing remains, also remove from package set
        if not packages[p].tarfiles and not packages[p].hints:
            ex_packages.append(p)

    # (modify package set outside of iteration over it)
    for p in ex_packages:
        logging.debug("removing package '%s' from package set" % (p))
        del packages[p]


#
# helper function to mark a package version as fresh (not stale)
#
@unique
class Freshness(IntEnum):
    # ordered most dominant first
    fresh = 1
    conditional = 2  # fresh, if other install packages from this source are fresh, stale otherwise
    stale = 3


def mark_package_fresh(packages, p, v, mark=Freshness.fresh):
    packages[p].tar(v).fresh = mark


#
# helper function evaluate if package needs marking for conditional retention
#

def mark_fn(packages, po, v, certain_age, obs_threshold, vault_requests):
    pn = po.name
    bv = po.best_version

    # 'conditional' package retention means the package is weakly retained.
    # This allows total expiry when a source package no longer provides
    # anything useful:
    #
    # - if all we have is a source package and a debuginfo package, then we
    # shouldn't retain anything.
    #
    if pn.endswith('-debuginfo'):
        return (Freshness.conditional, False)

    # - shared library packages which don't come from the current version of
    # source (i.e. is superseded or removed), have no packages from a
    # different source package which depend on them, and are over a certain
    # age
    #
    es = po.version_hints[bv].get('external-source', None)
    if (re.match(common_constants.SOVERSION_PACKAGE_RE, pn) and
        not any(packages[p].srcpackage(packages[p].best_version) != es for p in po.rdepends)):
        if es and (packages[es].best_version != bv):
            mtime = po.tar(v).mtime
            if mtime < certain_age:
                logging.debug("deprecated soversion package '%s' version '%s' mtime '%s' is over cut-off age" % (pn, v, time.strftime("%F %T %Z", time.localtime(mtime))))
                return (Freshness.conditional, True)

    # - package is an old-style obsoletion, over a certain age, and not marked
    # as self-destruct
    #
    if '_obsolete' in po.version_hints[v]['category']:
        mtime = po.tar(v).mtime
        if mtime < obs_threshold:
            provides = po.version_hints[v].get('provides', [])
            if '_self-destruct' not in provides:
                logging.debug("obsolete package '%s' version '%s' mtime '%s' is over cut-off age" % (pn, v, time.strftime("%F %T %Z", time.localtime(mtime))))
                return (Freshness.conditional, False)

    # - if package depends on anything in expired_provides
    #
    requires = po.version_hints[v].get('depends', [])
    if any(ep in requires for ep in past_mistakes.expired_provides):
        logging.debug("package '%s' version '%s' not retained as it requires a provide known to be expired" % (pn, v))
        return (Freshness.conditional, False)

    # - marked via 'calm-tool vault'
    #
    es = po.srcpackage(v, suffix=False)
    if es in vault_requests:
        if v in vault_requests[es]:
            logging.info("package '%s' version '%s' not retained due vault request" % (pn, v))
            return (Freshness.conditional, False)

    # otherwise, make no change
    return (Freshness.fresh, False)


#
# construct a move list of stale packages
#

SO_AGE_THRESHOLD_YEARS = 5
OBSOLETE_AGE_THRESHOLD_YEARS = 7


def stale_packages(packages, vault_requests):
    certain_age = time.time() - (SO_AGE_THRESHOLD_YEARS * 365.25 * 24 * 60 * 60)
    logging.debug("cut-off date for soversion package to be considered old is %s" % (time.strftime("%F %T %Z", time.localtime(certain_age))))

    obs_threshold = time.time() - (OBSOLETE_AGE_THRESHOLD_YEARS * 365.25 * 24 * 60 * 60)
    logging.debug("cut-off date for obsolete package to be considered old is %s" % (time.strftime("%F %T %Z", time.localtime(obs_threshold))))

    # mark install packages for freshness
    for pn, po in packages.items():
        # mark as fresh any versions explicitly listed in the keep: override
        # hint (unconditionally)
        for v in po.override_hints.get('keep', '').split():
            if v in po.versions():
                mark_package_fresh(packages, pn, v)
            else:
                logging.error("package '%s' has non-existent keep: version '%s'" % (pn, v))

        # mark as fresh the highest n non-test versions, where n is given by the
        # keep-count: override hint, (defaulting to DEFAULT_KEEP_COUNT)
        keep_count = int(po.override_hints.get('keep-count', common_constants.DEFAULT_KEEP_COUNT))
        for v in sorted(po.versions(), key=lambda v: SetupVersion(v), reverse=True):
            if 'test' not in po.version_hints[v]:
                if keep_count <= 0:
                    break
                mark_package_fresh(packages, pn, v)
                keep_count = keep_count - 1

        # mark as fresh the highest n test versions, where n is given by the
        # keep-count-test: override hint, (defaulting to DEFAULT_KEEP_COUNT_TEST)
        #
        # only consider versions not superseded by non-test versions (unless
        # 'keep-superseded-test' is present).
        keep_count = int(po.override_hints.get('keep-count-test', common_constants.DEFAULT_KEEP_COUNT_TEST))
        for v in sorted(po.versions(), key=lambda v: SetupVersion(v), reverse=True):
            if 'test' in po.version_hints[v]:
                if keep_count <= 0:
                    break
                mark_package_fresh(packages, pn, v)
                keep_count = keep_count - 1
            else:
                if 'keep-superseded-test' not in po.override_hints:
                    break

        # mark as fresh all versions after the first one which is newer than
        # the keep-days: override hint, (defaulting to DEFAULT_KEEP_DAYS)
        # (as opposed to checking the mtime for each version to determine if
        # it is included)
        keep_days = po.override_hints.get('keep-days', common_constants.DEFAULT_KEEP_DAYS)
        newer = False
        for v in sorted(po.versions(), key=lambda v: SetupVersion(v)):
            if not newer:
                if po.tar(v).mtime > (time.time() - (keep_days * 24 * 60 * 60)):
                    newer = True

            if newer:
                mark_package_fresh(packages, pn, v)

    for pn, po in packages.items():
        if po.kind != Kind.binary:
            continue

        # overwrite with 'conditional' package retention mark if it meets
        # various criteria
        for v in sorted(po.versions(), key=lambda v: SetupVersion(v)):
            (mark, others) = mark_fn(packages, po, v, certain_age, obs_threshold, vault_requests)
            if mark != Freshness.fresh:
                mark_package_fresh(packages, pn, v, mark)

            # also look over the other install packages generated by the
            # source...
            if others:
                es = po.version_hints[v].get('external-source', None)
                if es:
                    es_po = packages[es]
                    # ... if the source package version doesn't count as kept ...
                    #
                    # (set above using same critera as for install package
                    # e.g. we have an excess number of packages)
                    logging.debug("considering other packages from source package '%s' version '%s'" % (es, v))
                    if getattr(es_po.tar(v), 'fresh', Freshness.stale) != Freshness.fresh:
                        # ...  additionally mark anything with no other-source
                        # rdepends
                        for opn in sorted(es_po.is_used_by):
                            if v in packages[opn].versions():
                                if not any(packages[p].srcpackage(v) != es for p in packages[opn].rdepends):
                                    mark_package_fresh(packages, opn, v, mark)
                                else:
                                    logging.debug("package '%s' version '%s' retained due to being used" % (opn, v))

    # mark source packages as fresh if any install package which uses it is fresh
    for po in packages.values():
        if po.kind == Kind.source:
            for v in po.versions():
                mark = Freshness.stale
                for ip in po.is_used_by:
                    if v in packages[ip].versions():
                        mark = min(getattr(packages[ip].tar(v), 'fresh', Freshness.stale), mark)

                # if conditional is the best we could do, mark this source
                # package as stale, otherwise we are fresh
                if mark == Freshness.conditional:
                    mark = Freshness.stale

                po.tar(v).fresh = mark

                # update install packages which use this source package to the
                # matching state
                for ip in po.is_used_by:
                    if v in packages[ip].versions():
                        if getattr(packages[ip].tar(v), 'fresh', Freshness.stale) == Freshness.conditional:
                            packages[ip].tar(v).fresh = mark

    # build a move list of stale versions
    stale = MoveList()
    for pn, po in packages.items():
        all_stale = {}

        for v in sorted(po.versions(), key=lambda v: SetupVersion(v)):
            all_stale[v] = True
            if getattr(po.tar(v), 'fresh', Freshness.stale) != Freshness.fresh:
                to = po.tar(v)
                stale.add(*to.repopath.move())
                logging.debug("package '%s' version '%s' is stale" % (pn, v))
            else:
                all_stale[v] = False

        for v in po.hints:
            # if there's a pvr.hint without a fresh source or install of the
            # same version (including version: overrides), move it as well
            ov = po.hints[v].hints.get('original-version', v)
            if all_stale.get(v, True) and all_stale.get(ov, True):
                stale.add(*po.hints[v].repopath.move())
                logging.debug("package '%s' version '%s' hint is stale" % (pn, v))

        # clean up freshness mark
        for v in po.versions():
            try:
                delattr(po.tar(v), 'fresh')
            except AttributeError:
                pass

    return stale


#
#
#

if __name__ == "__main__":
    packages, _ = read_packages(common_constants.FTP)
    print("relarea has %d packages" % (len(packages)))
