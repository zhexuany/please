#!/usr/bin/env python
"""Pex building script for Please."""

import argparse
import json
import os
import pkg_resources
import shutil
import sys
import tempfile

from third_party.python.pex.pex_builder import PEXBuilder
from third_party.python.pex.interpreter import PythonInterpreter


def dereference_symlinks(src):
    """
    Resolve all symbolic references that `src` points to.  Note that this
    is different than `os.path.realpath` as path components leading up to
    the final location may still be symbolic links.
    """
    while os.path.islink(src):
        src = os.path.join(os.path.dirname(src), os.readlink(src))
    return src


def write_file(f, contents):
    """Writes contents to given file object.

    TODO(pebers):  This is mostly an effort to get around various encoding issues. Would be
                   nice to solve this in a less brutal way but not sure how best to resolve
                   python2 / python3 / pkg_resources :(
    """
    try:
        f.write(contents)
    except (UnicodeDecodeError, UnicodeEncodeError, TypeError):
        try:
            f.write(contents.decode('utf-8'))
        except (UnicodeDecodeError, UnicodeEncodeError):
            f.write(contents.decode('latin_1'))  # This is desperate peasantry of course.


def add_directory(root_path, path, pex_builder):
    """Recursively adds the contents of a directory to the pex."""
    base = os.path.join(root_path, path)
    for filename in os.listdir(base):
        src = dereference_symlinks(os.path.join(base, filename))
        dst = os.path.join(path, filename)
        if os.path.isdir(src):
            add_directory(root_path, dst, pex_builder)
        elif filename.endswith('.py'):
            # Assume this is source code and add as such.
            # TODO(pebers): this is possibly a bit simplistic...
            pex_builder.add_source(src, dst)
        elif not filename.endswith('.pyc'):
            pex_builder.add_resource(src, dst)


def add_test_files(test_sources, out_dir):
    """Add files required for running a test pex."""
    contents = pkg_resources.resource_string('src.build.python', 'test_main.py').decode('utf-8')
    remove_ext = lambda x: x[:-3] if x.endswith('.py') else x
    test_modules = ','.join(remove_ext(src.replace('/', '.')) for src in test_sources.split(','))
    contents = contents.replace('__TEST_NAMES__', test_modules)
    with open(os.path.join(out_dir, 'test_main.py'), 'w') as f:
        write_file(f, contents)
    # Extract the xmlrunner and coverage files too
    extract_directory('third_party.python', 'xmlrunner', out_dir, '.bootstrap/xmlrunner', True)
    extract_file('third_party.python', os.path.join(out_dir, '.bootstrap'), 'six.py', True)
    extract_directory('third_party.python', 'coverage', out_dir, '.bootstrap/coverage', True)


def add_main(module_dir, entry_point, out_dir):
    """Add pex_main.py as the entry point to a pex."""
    contents = pkg_resources.resource_string('src.build.python', 'pex_main.py').decode('utf-8')
    contents = contents.replace('__MODULE_DIR__', module_dir).replace('__ENTRY_POINT__', entry_point)
    with open(os.path.join(out_dir, 'pex_main.py'), 'w') as f:
        write_file(f, contents)
    return 'pex_main'


def extract_directory(in_pkg, in_req, out_dir, out_path, duplicates_allowed=False):
    """Extracts a directory from the pex builder and adds it to the staging directory."""
    target_dir = os.path.join(out_dir, out_path)
    if os.path.exists(target_dir) and duplicates_allowed:
        return
    os.makedirs(target_dir)
    full_dir = '%s.%s' % (in_pkg, in_req)
    for filename in pkg_resources.resource_listdir(in_pkg, in_req):
        if not pkg_resources.resource_isdir(full_dir, filename):
            extract_file(full_dir, target_dir, filename)


def extract_file(in_pkg, out_dir, filename, duplicates_allowed=False):
    """Extracts a single file from the pex builder and adds it to the staging directory."""
    target = os.path.join(out_dir, filename)
    if os.path.exists(target) and duplicates_allowed:
        return
    with open(target, 'w') as f:
        write_file(f, pkg_resources.resource_string(in_pkg, filename))


def main(args):
    # Pex doesn't support relative interpreter paths.
    # In python3 we could use shutil.which() to locate it but it's probably better
    # to just force the user to specify it.
    if not args.interpreter.startswith('/'):
        print('--interpreter argument must be an absolute path')
        sys.exit(1)

    # Add pkg_resources and the bootstrapper
    extract_directory('third_party.python',
                      'pex',
                      args.src_dir,
                      '.bootstrap/_pex')
    extract_file('third_party.python',
                 os.path.join(args.src_dir, '.bootstrap'),
                 'pkg_resources.py')

    # Setup a temp dir that the PEX builder will use as its scratch dir.
    tmp_dir = tempfile.mkdtemp()
    try:
        interpreter = PythonInterpreter.from_binary(args.interpreter)
        pex_builder = PEXBuilder(path=tmp_dir, interpreter=interpreter)
        pex_builder.info.zip_safe = args.zip_safe

        # Set the entry point.
        pex_main = add_main(args.module_dir, args.entry_point, args.src_dir)
        if args.test_package:
            # Stick with the test main, it knows what to do.
            pex_builder.info.entry_point = args.entry_point
        else:
            # Override the entry point so the module import override works.
            pex_builder.info.entry_point = pex_main

        if args.test_package and args.test_srcs:
            # Temp hack to make this work from an unexpected module name
            sys.path.append(os.path.join(sys.argv[0], 'third_party/python'))
            add_test_files(args.test_srcs, args.src_dir)

        # Add everything under the input dir.
        add_directory(args.src_dir, '.', pex_builder)

        # This function does some setuptools malarkey which is vexing me, so
        # I'm just gonna cheekily disable it for now.
        pex_builder._prepare_bootstrap = lambda: None

        # Generate the PEX file.
        pex_builder.build(args.out)

    # Always try cleaning up the scratch dir, ignoring failures.
    finally:
        shutil.rmtree(tmp_dir, True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--src_dir', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--entry_point', required=True)
    parser.add_argument('--interpreter', default=sys.executable)
    parser.add_argument('--test_package')
    parser.add_argument('--test_srcs')
    parser.add_argument('--module_dir', default='')
    parser.add_argument('--zip_safe', dest='zip_safe', action='store_true')
    parser.add_argument('--nozip_safe', dest='zip_safe', action='store_false')
    parser.set_defaults(zip_safe=True)
    main(parser.parse_args())