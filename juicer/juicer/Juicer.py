# -*- coding: utf-8 -*-
# Juicer - Administer Pulp and Release Carts
# Copyright © 2012, Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from juicer.common import Constants
import juicer.common.Cart
import juicer.juicer
import juicer.utils
import juicer.utils.Upload
import os
import re
import tempfile


class Juicer(object):
    def __init__(self, args):
        self.args = args

        (self.connectors, self._defaults) = juicer.utils.get_login_info()

        if 'environment' in self.args:
            for env in self.args.environment:
                try:
                    self.connectors[env].get()
                except Exception:
                    juicer.utils.Log.log_error("%s is not a server configured in juicer.conf" % env)
                    juicer.utils.Log.log_debug("Exiting...")
                    exit(1)

    # this is used to upload carts to pulp
    def upload(self, env, cart):
        """
        Nothing special happens here. This method recieves a
        destination repo, and a payload of `cart` which will be
        uploaded into the target repo.

        Preparation: To use this method you must pre-process your
        cart: Remotes must be fetched and saved locally. Directories
        must be recursed and replaced with their contents. Items
        should be signed if necessary.

        Warning: this method trusts you to Do The Right Thing (TM),
        ahead of time and check file types before feeding them to it.

        `env` - name of the environment with the cart destination
        `cart` - cart to upload
        """
        for repo in cart.repos():
            if not juicer.utils.repo_exists_p(repo, self.connectors[env], env):
                juicer.utils.Log.log_info("repo '%s' doesn't exist in %s environment... skipping!",
                                          (repo, env))
                continue

            repoid = "%s-%s" % (repo, env)
            juicer.utils.Log.log_debug("Beginning upload into %s repo" % repoid)

            for item in cart[repo]:
                juicer.utils.Log.log_info("Initiating upload of '%s' into '%s'" % (item.path, repoid))
                rpm_id = juicer.utils.upload_rpm(item.path, repoid, self.connectors[env])
                juicer.utils.Log.log_debug('%s uploaded with an id of %s' %
                                           (os.path.basename(item.path), rpm_id))

            # Upload carts aren't special, don't update their paths
            if cart.name == 'upload-cart':
                continue

            # Set the path to items in this cart to their location on
            # the pulp server.
            for item in cart[repo]:
                path = juicer.utils.remote_url(self.connectors[env],
                                               env,
                                               repo,
                                               os.path.basename(item.path))
                item.update(path)

        # Upload carts don't persist
        if not cart.name == 'upload-cart':
            cart.save()
            self.publish(cart)
        return True

    def push(self, cart, env=None):
        """
        `cart` - Release cart to push items from

        Pushes the items in a release cart to the pre-release environment.
        """
        juicer.utils.Log.log_debug("Initializing push of cart '%s'" % cart.name)

        if not env:
            env = self._defaults['start_in']

        self.sign_cart_for_env_maybe(cart, env)
        self.upload(env, cart)
        return True

    def publish(self, cart, env=None):
        """
        `cart` - Release cart to publish in json format

        Publish a release cart in JSON format to the pre-release environment.
        """

        juicer.utils.Log.log_debug("Initializing publish of cart '%s'" % cart.name)

        if not env:
            env = self._defaults['start_in']

        cart_file = os.path.join(juicer.common.Cart.CART_LOCATION, cart.name)

        if not cart_file.endswith('.json'):
            cart_file += '.json'

        juicer.utils.Log.log_debug("Initializing upload of cart '%s' to cart repository" % cart.name)

        repoid = "carts-%s" % env
        file_id = juicer.utils.upload_file(cart_file, repoid, self.connectors[env])
        juicer.utils.Log.log_debug('%s uploaded with an id of %s' %
                                   (os.path.basename(cart_file), file_id))
        return True

    def create_manifest(self, cart_name, manifest, query='/services/search/packages/'):
        """
        `cart_name` - Name of this release cart
        `manifest` - str containing path to manifest file
        """
        start_in = self._defaults['start_in']
        env_re = re.compile('.*-%s' % start_in)

        cart = juicer.common.Cart.Cart(cart_name)
        try:
            pkg_list = juicer.utils.parse_manifest(manifest)
        except IOError as e:
            juicer.utils.Log.log_error(e.message)
            exit(1)

        urls = {}

        # packages need to be included in every repo they're in
        for pkg in pkg_list:
            juicer.utils.Log.log_debug("Finding %s %s %s ..." % \
                    (pkg['name'], pkg['version'], pkg['release']))

            data = {'name': pkg['name'],
                    'version': pkg['version'],
                    'release': pkg['release']}

            _r = self.connectors[start_in].post(query, data)

            if not _r.status_code == Constants.PULP_POST_OK:
                juicer.utils.Log.log_error('%s was not found in pulp. Additionally, a %s status code was returned' % (pkg['name']._r.status_code))
                exit(1)

            content = juicer.utils.load_json_str(_r.content)

            if len(content) == 0:
                juicer.utils.Log.log_debug("Searching for %s returned 0 results." % pkg['name'])
                continue

            ppkg = content[0]

            for repo in ppkg['repoids']:
                if re.match(env_re, repo):
                    if repo not in urls:
                        urls[repo] = []

                    pkg_url = juicer.utils.remote_url(self.connectors[start_in],
                        start_in, repo, ppkg['filename'])
                    urls[repo].append(pkg_url)

        for repo in urls:
            cart[repo] = urls[repo]

        cart.save()
        return cart

    def create(self, cart_name, cart_description):
        """
        `cart_name` - Name of this release cart
        `cart_description` - list of ['reponame', item1, ..., itemN] lists
        """
        cart = juicer.common.Cart.Cart(cart_name)

        # repo_items is a list that starts with the REPO name,
        # followed by the ITEMS going into the repo.
        for repo_items in cart_description:
            (repo, items) = (repo_items[0], repo_items[1:])
            juicer.utils.Log.log_debug("Processing %s input items for repo '%s'." % (len(items), repo))
            cart[repo] = items

        cart.save()
        return cart

    def show(self, cart_name):
        cart = juicer.common.Cart.Cart(cart_name)
        cart.load(cart_name)
        return str(cart)

    def search(self, name='', search_carts=False, query='/services/search/packages/'):
        data = {'regex': True,
                'name': name}

        juicer.utils.Log.log_info('Packages:')

        for env in self.args.environment:
            juicer.utils.Log.log_debug("Querying %s server" % env)
            _r = self.connectors[env].post(query, data)

            if not _r.status_code == Constants.PULP_POST_OK:
                juicer.utils.Log.log_debug("Expected PULP_POST_OK, got %s", _r.status_code)
                _r.raise_for_status()

            juicer.utils.Log.log_info('%s:' % str.upper(env))

            pkg_list = juicer.utils.load_json_str(_r.content)

            for package in pkg_list:
                # if the package is in a repo, show a link to the package in said repo
                # otherwise, show nothing
                if len(package['repoids']) > 0:
                    target = package['repoids'][0]

                    _r = self.connectors[env].get('/repositories/%s/' % target)
                    if not _r.status_code == Constants.PULP_GET_OK:
                        juicer.utils.Log.error_log("%s was not found as a repoid. A %s status code was returned" %
                                (target, _r.status_code))
                        exit(1)
                    repo = juicer.utils.load_json_str(_r.content)['name']

                    link = juicer.utils.remote_url(self.connectors[env], env, repo, package['filename'])
                else:
                    link = ''

                juicer.utils.Log.log_info('%s %s %s' % (package['name'], package['version'], link))

        if search_carts:
            # if the package is in a cart, show the cart name
            juicer.utils.Log.log_info('\nCarts:')

            start_in = self._defaults['start_in']
            remote = '/repositories/carts-%s/files/' % start_in
            regex = re.compile('.*%s.*' % name)

            _r = self.connectors[start_in].get(remote)
            if not _r.status_code == Constants.PULP_GET_OK:
                raise IOError("Couldn't get cart list")

            cart_list = juicer.utils.load_json_str(_r.content)

            for cart in cart_list:
                cname = cart['filename'].rstrip('.json')
                repos_items = juicer.utils.get_cart(self.connectors[start_in].base_url, start_in, cname)['repos_items']

                for repo in repos_items:
                    for items in repos_items[repo]:
                        if re.match(regex, items):
                            juicer.utils.Log.log_info(cname)

    def hello(self):
        """
        Test pulp server connections defined in ~/.juicer.conf.
        """
        for env in self.args.environment:
            juicer.utils.Log.log_info("Trying to open a connection to %s, %s ...",
                                      env, self.connectors[env].base_url)
            try:
                _r = self.connectors[env].get()
                juicer.utils.Log.log_info("OK")
            except Exception:
                juicer.utils.Log.log_info("FAILED")
                continue

            juicer.utils.Log.log_info("Attempting to authenticate as %s",
                                      self.connectors[env].auth[0])

            _r = self.connectors[env].get('/repositories/')

            if _r.status_code == Constants.PULP_GET_OK:
                juicer.utils.Log.log_info("OK")
            else:
                juicer.utils.Log.log_info("FAILED")
                juicer.utils.Log.log_info("Server said: %s", _r.content)
                continue
        return True

    def pull(self, cartname=None, env=None):
        """
        `cartname` - Name of cart

        Pull remote cart from the pre release (base) environment
        """
        if not env:
            env = self._defaults['start_in']
        juicer.utils.Log.log_debug("Initializing pulling cart: %s ...", cartname)
        cart_file = os.path.join(juicer.common.Cart.CART_LOCATION, cartname)
        cart_file += '.json'
        juicer.utils.save_url_as(juicer.utils.remote_url(self.connectors[env], env, 'carts', cartname + '.json'),
                                 cart_file)
        juicer.utils.Log.log_info("pulled cart %s and saved to %s", cartname, cart_file)
        return True

    def promote(self, name):
        """
        `name` - name of cart

        Promote a cart from its current environment to the next in the chain.
        """
        cart = juicer.common.Cart.Cart(name=name, autoload=True, autosync=True)
        old_env = cart.current_env
        cart.current_env = juicer.utils.get_next_environment(cart.current_env)

        juicer.utils.Log.log_debug("Syncing down rpms...")
        cart.sync_remotes()
        self.sign_cart_for_env_maybe(cart, cart.current_env)

        juicer.utils.Log.log_info("Promoting %s from %s to %s" %
                (name, old_env, cart.current_env))

        for repo in cart.repos():
            juicer.utils.Log.log_debug("Promoting %s to %s in %s" %
                                       (cart[repo], repo, cart.current_env))
            self.upload(cart.current_env, cart)

        cart.save()

        self.publish(cart)

    def sign_cart_for_env_maybe(self, cart, env=None):
        """
        Sign the items to upload, if the env requires a signature.

        `cart` - Cart to sign
        `envs` - The cart is signed if env has the property:
        requires_signature = True

        Will attempt to load the rpm_sign_plugin defined in
        ~/.juicer.conf, which must be a plugin inheriting from
        juicer.common.RpmSignPlugin. If available, we'll call
        cart.sign_items() with a reference to the
        rpm_sign_plugin.sign_rpms method.
        """
        if not self.connectors[env].requires_signature:
            return None

        juicer.utils.Log.log_notice("%s requires RPM signatures", env)
        juicer.utils.Log.log_notice("Checking for rpm_sign_plugin definition ...")
        module_name = self._defaults['rpm_sign_plugin']
        if self._defaults['rpm_sign_plugin']:
            juicer.utils.Log.log_notice("Found rpm_sign_plugin definition: %s",
                                        self._defaults['rpm_sign_plugin'])
            juicer.utils.Log.log_notice("Attempting to load ...")

            try:
                rpm_sign_plugin = __import__(module_name, fromlist=[module_name])
                juicer.utils.Log.log_notice("Successfully loaded %s ...", module_name)
                plugin_object = getattr(rpm_sign_plugin, module_name.split('.')[-1])
                signer = plugin_object()
                cart.sign_items(signer.sign_rpms)
            except ImportError as e:
                juicer.utils.Log.log_notice("there was a problem using %s ... error: %s",
                                            module_name, e)
        else:
            juicer.utils.Log.log_info("did not find an rpm_sign_plugin!")
        return True
