#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Patch k8s_agent.py on build host: fix script_args so flags are passed as
# separate argv entries (e.g. ["--scheme","B","--hostname","x"]) instead of a
# single string like "--scheme B". Single-string form breaks arg-parsing shells.
import io

PATH = '/home/bontor/agent-build/Agent-master/core/k8s_agent.py'
with io.open(PATH, 'r', encoding='utf-8') as f:
    src = f.read()

old = '''        script_args = []
        if scheme:
            script_args.append(f"--scheme {scheme}")
        if hostname:
            script_args.append(f"--hostname {hostname}")
        if nodes:
            script_args.append(f'--nodes "{nodes}"')
        if ssh_pass:
            script_args.append(f'--ssh-pass "{ssh_pass}"')
        if deps_dir:
            script_args.append(f"--offline-dir {deps_dir}")'''

new = '''        script_args = []
        # Pass each flag and its value as separate argv entries so argument-parsing
        # scripts receive $1=--scheme, $2=B etc. (not a single "--scheme B" token).
        if scheme:
            script_args.append("--scheme")
            script_args.append(scheme)
        if hostname:
            script_args.append("--hostname")
            script_args.append(hostname)
        if nodes:
            script_args.append("--nodes")
            script_args.append(nodes)
        if ssh_pass:
            script_args.append("--ssh-pass")
            script_args.append(ssh_pass)
        if deps_dir:
            script_args.append("--offline-dir")
            script_args.append(deps_dir)'''

if old in src:
    src = src.replace(old, new)
    print('arg-fix applied')
else:
    print('arg-fix NOT FOUND - check exact whitespace')

with io.open(PATH, 'w', encoding='utf-8') as f:
    f.write(src)

import py_compile
py_compile.compile(PATH, doraise=True)
print('py_compile OK')
