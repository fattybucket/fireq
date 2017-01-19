import argparse
import asyncio
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import root, log, Repo, conf, utils

dry_run = False
ssh_opts = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'


def remote(cmd, host, opts=ssh_opts):
    cmd = cmd.replace('"', '\\"')
    return (
        'set -e;rsync -ahv --delete'
        ' -e \'ssh {opts}\''
        ' --exclude=.git --exclude=logs --exclude=env'
        ' {root}/ {host}:{newroot}/;'
        'ssh {opts} {host} "cd {newroot}; {cmd}"'
        .format(
            newroot='/opt/superdesk-fire',
            root=root,
            host=host,
            opts=opts,
            cmd=cmd
        )
    )


def sh(cmd, params=None, ssh=None, exit=True):
    params = params or {}
    if ssh:
        cmd = remote(cmd, ssh)

    log.info(cmd)
    if dry_run:
        log.info('Dry run!')
        return 0

    code = subprocess.call(cmd, executable='/bin/bash', shell=True)
    if exit and code:
        raise SystemExit(code)
    return code


def run_async(fn, *a, **kw):
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(fn(*a, **kw))
    loop.close()
    return result


def build(short_name, ref, sha, by_url=None, **opts):
    only_web = opts.pop('only_web', None)
    only_checks = opts.pop('only_checks', None)

    if by_url:
        url = utils.get_restart_url(short_name, ref)
        req = urllib.request.Request(url)
        try:
            resp = urllib.request.urlopen(req)
            log.info('%s: %s', resp.status, resp.reason)
        except Exception as e:
            log.error(e)
        return

    async def run():
        from . import build

        ctx = await build.get_ctx(Repo[short_name].value, ref, sha, **opts)
        if not ctx:
            return 1

        target = None
        if only_checks:
            ctx['install'] = False
            target = build.checks
        elif only_web:
            target = build.www
        else:
            target = build.build

        return await target(ctx)

    code = run_async(run)
    log.info(code)
    raise SystemExit(code)


def gh_request(path):
    path = Path(path) / 'request.json'
    req = json.loads(path.read_text())

    if isinstance(req, dict):
        # TODO: remove it later
        # this format was used at the begining
        return req['headers'], req['json']
    else:
        return req


def gh_build(path, url):
    headers, body = gh_request(path)
    if url:
        data = json.dumps(body, indent=2, sort_keys=True).encode()
        headers['X-Hub-Signature'] = utils.get_signature(data)
        headers['Content-Length'] = len(data)
        req = urllib.request.Request(url, data, headers)
        try:
            resp = urllib.request.urlopen(req)
            log.info('%s: %s', resp.status, resp.reason)
        except Exception as e:
            log.error(e)

    async def run():
        from . import web

        ctx = await web.get_hook_ctx(headers, body, clean=True)
        return await web.build(ctx)

    code = run_async()
    log.info(code)
    raise SystemExit(code)


def gh_clean():
    """
    Remove contaner if
    - pull request was closed
    - branch was removed
    otherwise keep container alive
    """
    def skip(prefix, ref, sha):
        name = re.sub('[^a-z0-9]', '', str(ref))
        name = '%s-%s' % (prefix, name)
        skips.append('^%s$' % name)
        skips.append('^%s-%s' % (name, sha[:10]))

    skips = []
    for repo in Repo:
        for i in gh_api('%s/branches' % repo.value):
            skip(repo.name, i['name'], i['commit']['sha'])
        for i in gh_api('%s/pulls?state=open' % repo.value):
            skip(repo.name + 'pr', i['number'], i['head']['sha'])

    skips = '(%s)' % '|'.join(skips)
    clean = [n for n in lxc_ls() if not re.match(skips, n)]
    cmd = ';\n'.join('./fire lxc-rm %s' % n for n in clean)
    sh('\n%s;\n./fire nginx' % cmd)


def update_nginx(path, names=None, ssl=False, live=False):
    cmd = ''
    if not names:
        names = [name for name in lxc_ls(www=True)]
        # move sd-master, the first name uses for cert filename
        names.insert(0, names.pop(names.index('sd-master')))

    if ssl:
        domains = ','.join('%s.%s' % (n, conf['domain']) for n in names)
        staging = (
            '' if live
            else '--server https://acme-staging.api.letsencrypt.org/directory'
        )
        sh(
            'time su - -c "'
            '  /opt/scripts/certbot-auto certonly '
            '   --agree-tos --non-interactive --expand'
            '   --email=support@sourcefabric.org'
            '   --webroot -w /var/tmp -d {domains}'
            '   {staging}'
            '"'.format(domains=domains, staging=staging),
            exit=False
        )

    cmd = '\n'.join(
        'name={name} host={name}.{domain} '
        '. endpoints/superdesk-dev/nginx.tpl;'
        .format(name=name, domain=conf['domain'])
        for name in names
    )
    sh(
        '({cmd}) > {path};'
        'nginx -s reload'
        .format(cmd=cmd, path=path)
    )


def gh_api(url, exc=True):
    if not url.startswith('https://'):
        url = 'https://api.github.com/repos/' + url
    try:
        req = urllib.request.Request(url, headers=utils.gh_auth())
        res = urllib.request.urlopen(req)
        log.info('%s: %s', url, res.status)
        return json.loads(res.read().decode())
    except urllib.error.URLError as e:
        log.info('%s: %s', url, e)
        if exc:
            raise
    return None


def lxc_ls(*, www=False):
    opts = '--running' if www else ''
    names = subprocess.check_output('lxc-ls -1 %s' % opts, shell=True)
    names = names.decode().split()
    pattern = '^sd[a-z]*-[a-z0-9]+'
    if www:
        pattern += '$'
    return [n for n in names if re.match(pattern, n)]


def main():
    global dry_run

    parser = argparse.ArgumentParser('fire')
    cmds = parser.add_subparsers(help='commands')

    def cmd(name, **kw):
        p = cmds.add_parser(name, **kw)
        p.set_defaults(cmd=name)
        p.arg = lambda *a, **kw: p.add_argument(*a, **kw) and p
        p.exe = lambda f: p.set_defaults(exe=f) and p

        p.arg('--dry-run', action='store_true')
        return p

    def ssh(ssh, lxc_name):
        return (
            ssh or
            (lxc_name and '$(lxc-info -n %s -iH)' % lxc_name)
        )

    cmd('install', aliases=['i'])\
        .arg('--lxc-name')\
        .arg('--ssh')\
        .arg('-e', '--endpoint', default='superdesk/master')\
        .arg('--prepopulate', action='store_true')\
        .arg('--services', action='store_true')\
        .arg('--env', default='')\
        .exe(lambda a: sh(
            'cd endpoints;'
            'services={services} '
            'prepopulate={prepopulate} '
            'action=do_install '
            '{env} '
            '{endpoint}'
            .format(
                services=a.services or '',
                prepopulate=a.prepopulate or '',
                endpoint=a.endpoint,
                env=a.env
            ),
            ssh=ssh(a.ssh, a.lxc_name)
        ))

    cmd('run', aliases=['r'])\
        .arg('--lxc-name')\
        .arg('--ssh')\
        .arg('-e', '--endpoint', default='superdesk/master')\
        .arg('-a', '--action', default='')\
        .arg('--env', default='')\
        .exe(lambda a: sh(
            'cd endpoints;'
            'action={action!r} {env} {endpoint}'
            .format(
                action=a.action,
                endpoint=a.endpoint,
                env=a.env
            ),
            ssh=ssh(a.ssh, a.lxc_name)
        ))

    cmd('build')\
        .arg('short_name', choices=[i.name for i in Repo])\
        .arg('ref')\
        .arg('--sha')\
        .arg('-u', '--by-url', action='store_true')\
        .arg('-c', '--only-checks', action='store_true')\
        .arg('-w', '--only-web', action='store_true')\
        .arg('--env', default='')\
        .arg('--clean', action='store_true')\
        .arg('--statuses', action='store_true')\
        .arg('--e2e-chunks', type=int, default=conf['e2e_chunks'])\
        .exe(lambda a: build(
            a.short_name, a.ref, a.sha, a.by_url,
            only_checks=a.only_checks,
            only_web=a.only_web,
            clean=a.clean,
            e2e_chunks=a.e2e_chunks,
            no_statuses=not a.statuses,
            env=a.env,
        ))

    cmd('gh-build')\
        .arg('path')\
        .arg('-u', '--url')\
        .exe(lambda a: gh_build(a.path, a.url))\

    cmd('gh-clean', help='remove unused containers')\
        .exe(lambda a: gh_clean())

    cmd('nginx', help='update nginx config')\
        .arg('-p', '--path', default='/etc/nginx/sites-enabled/sd')\
        .arg('-n', '--name', action='append')\
        .arg('--ssl', action='store_true')\
        .arg('--live', action='store_true')\
        .exe(lambda a: update_nginx(a.path, a.name, a.ssl, a.live))

    cmd('gen-files')\
        .exe(lambda a: sh('bin/gen-files.sh'))

    cmd('lxc-init')\
        .arg('-n', '--name', default='sd0')\
        .arg('--rm', action='store_true')\
        .arg('-k', '--keys', default='/root/.ssh/id_rsa.pub')\
        .arg('-o', '--opts', default='')\
        .exe(lambda a: sh(
            'name={name} '
            'rm={rm} '
            'keys={keys} '
            'opts={opts!r} '
            'bin/lxc-init.sh'
            .format(name=a.name, rm=a.rm or '', opts=a.opts, keys=a.keys)
        ))

    cmd('lxc-base')\
        .arg('-n', '--name', default=conf['lxc_base'])\
        .arg('-p', '--path', default='/opt/superdesk')\
        .arg('-o', '--opts', default='-B zfs')\
        .arg('--no-services', action='store_true')\
        .exe(lambda a: sh(
            'set -ex;'
            'tmp={name}--tmp; '
            './fire lxc-init -n $tmp --rm -o {opts!r};'
            './fire i --lxc-name=$tmp -e superdesk-dev/master {services};'
            './fire lxc-ssh $tmp -c "rm -rf {path}";'
            './fire lxc-copy -rc --no-snapshot -b $tmp {name};'
            .format(
                name=a.name, path=a.path, opts=a.opts,
                services='' if a.no_services else '--services'
            )
        ))

    cmd('lxc-data')\
        .arg('-n', '--name', default=conf['lxc_data'])\
        .arg('-o', '--opts', default='')\
        .exe(lambda a: sh(
            'set -ex;'
            'tmp={name}--tmp; '
            './fire lxc-init -n $tmp --rm -o {opts!r};'
            './fire r --lxc-name=$tmp -e superdesk-dev/master -a do_services;'
            './fire lxc-copy -rcs --no-snapshot -b $tmp {name};'
            .format(name=a.name, opts=a.opts)
        ))

    cmd('lxc-expose')\
        .arg('-n', '--name', default='sd0')\
        .arg('-d', '--domain', required=True)\
        .arg('-c', '--clean', action='store_true')\
        .exe(lambda a: sh(
            'name={name} '
            'domain={domain} '
            'clean={clean} '
            'bin/lxc-expose.sh'
            .format(name=a.name, domain=a.domain, clean=a.clean or '')
        ))

    cmd('lxc-copy')\
        .arg('name')\
        .arg('-b', '--base', default=conf['lxc_base'])\
        .arg('--cpus', default='')\
        .arg('-c', '--clean', action='store_true')\
        .arg('-s', '--start', action='store_true')\
        .arg('-r', '--rename', action='store_true')\
        .arg('--no-snapshot', action='store_true')\
        .exe(lambda a: sh(
            'name={name} '
            'rename={rename} '
            'clean={clean} '
            'start={start} '
            'base={base} '
            'cpus={cpus} '
            'snapshot={snapshot} '
            'bin/lxc-copy.sh'
            .format(
                name=a.name,
                base=a.base,
                cpus=a.cpus,
                start=a.start or '',
                clean=a.clean or '',
                rename=a.rename or '',
                snapshot='' if a.no_snapshot else 1,
            )
        ))

    cmd('lxc-rm')\
        .arg('name', nargs='+')\
        .exe(lambda a: sh('; '.join(
            'lxc-destroy -f -n {name}'
            .format(name=name)
            for name in a.name if name
        )))

    cmd('lxc-ssh')\
        .arg('name')\
        .arg('-c', '--cmd', default='')\
        .exe(lambda a: sh(
            'ssh {ssh_opts} $(lxc-info -n {name} -iH) {cmd}'
            .format(ssh_opts=ssh_opts, name=a.name, cmd=a.cmd)
        ))

    cmd('lxc-wait')\
        .arg('name')\
        .exe(lambda a: sh(
            'sleep 3 && '
            'lxc-wait -n {name} -s RUNNING && '
            'while ! $(./fire lxc-ssh {name} -c true > /dev/null);'
            '   do sleep 1; '
            'done'
            .format(name=a.name)
        ))

    cmd('lxc-clean')\
        .arg('pattern', default='^sd', nargs='?')\
        .exe(lambda a: sh(
            'lxc-ls -1'
            '   | grep -e "{pattern}"'
            '   | sort -r'
            '   | xargs -r ./fire lxc-rm'
            .format(pattern=a.pattern)
        ))

    args = parser.parse_args()
    dry_run = getattr(args, 'dry_run', dry_run)
    if not hasattr(args, 'exe'):
        parser.print_usage()
    else:
        args.exe(args)
