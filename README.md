systemd-password-agent: python implementation of [systemd password agent interface](http://www.freedesktop.org/wiki/Software/systemd/PasswordAgents)
--------------------

Honest to $DEITY implementation of the interface in form of python script (for
ease of hackability), with all the inotify, timestamp, policykit goodies and
checks.


Usage
--------------------

* Clone.
* Copy the py script to some more-or-less stable path (like
  /usr/local/sbin/systemd_password_cache, just make sure it matches what's in the
  .service files).
* Override get_pass() function in the script with whatever you need to get the
  password, like:

    def get_pass():
      cache_path = '/run/initramfs/.password.cache'
      try: return open(cache_path, 'rb').read().strip()
      except (OSError, IOError): raise SkipRequest

* Install .service file to run the thing to some /etc/systemd/system path and
  enable it. Also skim through it and correct paths and requirements maybe.

Example usage (for which these scripts/services were actually written) - caching
of passphrase (or hardware-generated key) for encrypted (dm-crypt) disks
with dracut/systemd.
Idea is to use RSA key on a smartcard token to produce key for dm-crypt (or just
cache what is entered interactively, as plymouth does), then use it repeatedly
for every encrypted lvm partition systemd tries to unlock, not just root.

(more info on how this particular setup works can be found
[here](http://blog.fraggod.net/2011/10/dm-crypt-password-caching-between-dracut-and-systemd-systemd-password-agent))
