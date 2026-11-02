"""How the patcher reaches the printer over SSH. On macOS it runs the system ssh and scp and gives
them the password through an askpass script; Windows has no such way, so the Windows program
talks SSH itself through paramiko. QIDI_SSH=paramiko picks paramiko anywhere, for testing."""
import os
import posixpath
import subprocess
import sys
import tempfile

SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]


class LinkError(Exception):
    pass


def uses_paramiko():
    return sys.platform == "win32" or os.environ.get("QIDI_SSH") == "paramiko"


def open_link(user, host, password, known_hosts):
    """One connection for a run of commands: `with open_link(...) as link: link.run(...)`."""
    if uses_paramiko():
        return ParamikoLink(user, host, password, known_hosts)
    return CliLink(user, host, password)


class CliLink:
    def __init__(self, user, host, password):
        self.login = "%s@%s" % (user, host)
        self.password = password

    def __enter__(self):
        self.work = tempfile.TemporaryDirectory()
        askpass = os.path.join(self.work.name, "askpass")
        with open(askpass, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$PRINTER_PASSWORD"\n')
        os.chmod(askpass, 0o700)
        self.env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force",
                        PRINTER_PASSWORD=self.password)
        return self

    def __exit__(self, *exc):
        self.work.cleanup()

    def run(self, command, input=None, merge=False, timeout=None):
        """(exit code, output, errors); `merge` puts the errors into the output."""
        feed = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        done = subprocess.run(["ssh"] + SSH_OPTS + [self.login, command], env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              timeout=timeout, **feed)
        return done.returncode, done.stdout, done.stderr or ""

    def put_dir(self, local, remote):
        subprocess.run(["scp", "-q", "-r"] + SSH_OPTS + [local, self.login + ":" + remote], env=self.env,
                       check=True, stdin=subprocess.DEVNULL)

    def get(self, remote, local):
        subprocess.run(["scp", "-q"] + SSH_OPTS + [self.login + ":" + remote, str(local)], env=self.env,
                       check=True, stdin=subprocess.DEVNULL)


class ParamikoLink:
    """A new printer's key is remembered, a changed one is refused, as ssh's accept-new does."""

    def __init__(self, user, host, password, known_hosts):
        self.user, self.host, self.password = user, host, password
        self.known_hosts = str(known_hosts)

    def __enter__(self):
        import paramiko
        os.makedirs(os.path.dirname(self.known_hosts), exist_ok=True)
        if not os.path.exists(self.known_hosts):
            open(self.known_hosts, "a").close()
        self.client = paramiko.SSHClient()
        self.client.load_host_keys(self.known_hosts)
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(self.host, username=self.user, password=self.password, timeout=10,
                                banner_timeout=15, auth_timeout=15, allow_agent=False, look_for_keys=False)
        except paramiko.BadHostKeyException:
            self.client.close()
            raise LinkError("Ключ принтера %s не совпадает с прежним. Если принтер переустанавливали, "
                            "удали его строку из %s." % (self.host, self.known_hosts))
        except paramiko.AuthenticationException:
            self.client.close()
            raise LinkError("Принтер не принял логин и пароль.")
        except paramiko.SSHException as e:
            self.client.close()
            raise LinkError("SSH: %s" % (e,))
        return self

    def __exit__(self, *exc):
        self.client.close()

    def run(self, command, input=None, merge=False, timeout=None):
        import paramiko
        try:
            channel = self.client.get_transport().open_session()
            channel.set_combine_stderr(merge)
            channel.settimeout(timeout)
            channel.exec_command(command)
            if input is not None:
                channel.sendall(input.encode("utf-8"))
            channel.shutdown_write()
            output = channel.makefile("rb").read().decode("utf-8", "replace")
            errors = channel.makefile_stderr("rb").read().decode("utf-8", "replace")
            return channel.recv_exit_status(), output, errors
        except paramiko.SSHException as e:
            raise LinkError("SSH: %s" % (e,))

    def put_dir(self, local, remote):
        """Like scp -r: `remote` becomes a copy of the folder `local`."""
        with self._sftp() as sftp:
            for folder, _, files in os.walk(local):
                relative = os.path.relpath(folder, local)
                target = remote if relative == "." else posixpath.join(remote, *relative.split(os.sep))
                sftp.mkdir(target)
                for name in files:
                    sftp.put(os.path.join(folder, name), posixpath.join(target, name))

    def get(self, remote, local):
        with self._sftp() as sftp:
            sftp.get(remote, str(local))

    def _sftp(self):
        import paramiko
        try:
            return self.client.open_sftp()
        except paramiko.SSHException as e:
            raise LinkError("SFTP: %s" % (e,))


def make_key(path, comment):
    """An ed25519 key pair in OpenSSH format, where there is no ssh-keygen to make one."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                  serialization.NoEncryption()))
    with open(str(path) + ".pub", "wb") as f:
        f.write(key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
                + b" " + comment.encode() + b"\n")
