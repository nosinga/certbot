"""Nginx Configuration"""
import logging
import os
import re
import socket
import subprocess
import tempfile
import time

import OpenSSL
import six
import zope.interface

from acme import challenges
from acme import crypto_util as acme_crypto_util

from certbot import constants as core_constants
from certbot import crypto_util
from certbot import errors
from certbot import interfaces
from certbot import util

from certbot.plugins import common

from certbot_nginx import constants
from certbot_nginx import nginxparser
from certbot_nginx import parser
from certbot_nginx import tls_sni_01


logger = logging.getLogger(__name__)

REDIRECT_BLOCK = [
    ['\n    ', 'return', ' ', '301', ' ', 'https://$host$request_uri'],
    ['\n']
]

REDIRECT_COMMENT_BLOCK = [
    ['\n    ', '#', ' Redirect non-https traffic to https'],
    ['\n    ', '#', ' return 301 https://$host$request_uri;'],
    ['\n']
]

@zope.interface.implementer(interfaces.IAuthenticator, interfaces.IInstaller)
@zope.interface.provider(interfaces.IPluginFactory)
class NginxConfigurator(common.Installer):
    # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Nginx configurator.

    .. todo:: Add proper support for comments in the config. Currently,
        config files modified by the configurator will lose all their comments.

    :ivar config: Configuration.
    :type config: :class:`~certbot.interfaces.IConfig`

    :ivar parser: Handles low level parsing
    :type parser: :class:`~certbot_nginx.parser`

    :ivar str save_notes: Human-readable config change notes

    :ivar reverter: saves and reverts checkpoints
    :type reverter: :class:`certbot.reverter.Reverter`

    :ivar tup version: version of Nginx

    """

    description = "Nginx Web Server plugin - Alpha"

    DEFAULT_LISTEN_PORT = '80'

    @classmethod
    def add_parser_arguments(cls, add):
        add("server-root", default=constants.CLI_DEFAULTS["server_root"],
            help="Nginx server root directory.")
        add("ctl", default=constants.CLI_DEFAULTS["ctl"], help="Path to the "
            "'nginx' binary, used for 'configtest' and retrieving nginx "
            "version number.")

    @property
    def nginx_conf(self):
        """Nginx config file path."""
        return os.path.join(self.conf("server_root"), "nginx.conf")

    def __init__(self, *args, **kwargs):
        """Initialize an Nginx Configurator.

        :param tup version: version of Nginx as a tuple (1, 4, 7)
            (used mostly for unittesting)

        """
        version = kwargs.pop("version", None)
        super(NginxConfigurator, self).__init__(*args, **kwargs)

        # Verify that all directories and files exist with proper permissions
        self._verify_setup()

        # Files to save
        self.save_notes = ""

        # For creating new vhosts if no names match
        self.new_vhost = None

        # Add number of outstanding challenges
        self._chall_out = 0

        # These will be set in the prepare function
        self.parser = None
        self.version = version
        self._enhance_func = {"redirect": self._enable_redirect,
                              "staple-ocsp": self._enable_ocsp_stapling}

        self.reverter.recovery_routine()

    @property
    def mod_ssl_conf(self):
        """Full absolute path to SSL configuration file."""
        return os.path.join(self.config.config_dir, constants.MOD_SSL_CONF_DEST)

    @property
    def updated_mod_ssl_conf_digest(self):
        """Full absolute path to digest of updated SSL configuration file."""
        return os.path.join(self.config.config_dir, constants.UPDATED_MOD_SSL_CONF_DIGEST)

    # This is called in determine_authenticator and determine_installer
    def prepare(self):
        """Prepare the authenticator/installer.

        :raises .errors.NoInstallationError: If Nginx ctl cannot be found
        :raises .errors.MisconfigurationError: If Nginx is misconfigured
        """
        # Verify Nginx is installed
        if not util.exe_exists(self.conf('ctl')):
            raise errors.NoInstallationError

        # Make sure configuration is valid
        self.config_test()


        self.parser = parser.NginxParser(self.conf('server-root'))

        install_ssl_options_conf(self.mod_ssl_conf, self.updated_mod_ssl_conf_digest)

        self.install_ssl_dhparams()

        # Set Version
        if self.version is None:
            self.version = self.get_version()

        # Prevent two Nginx plugins from modifying a config at once
        try:
            util.lock_dir_until_exit(self.conf('server-root'))
        except (OSError, errors.LockError):
            logger.debug('Encountered error:', exc_info=True)
            raise errors.PluginError(
                'Unable to lock %s', self.conf('server-root'))

    # Entry point in main.py for installing cert
    def deploy_cert(self, domain, cert_path, key_path,
                    chain_path=None, fullchain_path=None):
        # pylint: disable=unused-argument
        """Deploys certificate to specified virtual host.

        .. note:: Aborts if the vhost is missing ssl_certificate or
            ssl_certificate_key.

        .. note:: This doesn't save the config files!

        :raises errors.PluginError: When unable to deploy certificate due to
            a lack of directives or configuration

        """
        if not fullchain_path:
            raise errors.PluginError(
                "The nginx plugin currently requires --fullchain-path to "
                "install a cert.")

        vhost = self.choose_vhost(domain, create_if_no_match=True)
        cert_directives = [['\n    ', 'ssl_certificate', ' ', fullchain_path],
                           ['\n    ', 'ssl_certificate_key', ' ', key_path]]

        self.parser.add_server_directives(vhost,
                                          cert_directives, replace=True)
        logger.info("Deployed Certificate to VirtualHost %s for %s",
                    vhost.filep, vhost.names)

        self.save_notes += ("Changed vhost at %s with addresses of %s\n" %
                            (vhost.filep,
                             ", ".join(str(addr) for addr in vhost.addrs)))
        self.save_notes += "\tssl_certificate %s\n" % fullchain_path
        self.save_notes += "\tssl_certificate_key %s\n" % key_path

    #######################
    # Vhost parsing methods
    #######################
    def choose_vhost(self, target_name, create_if_no_match=False):
        """Chooses a virtual host based on the given domain name.

        .. note:: This makes the vhost SSL-enabled if it isn't already. Follows
            Nginx's server block selection rules preferring blocks that are
            already SSL.

        .. todo:: This should maybe return list if no obvious answer
            is presented.

        .. todo:: The special name "$hostname" corresponds to the machine's
            hostname. Currently we just ignore this.

        :param str target_name: domain name
        :param bool create_if_no_match: If we should create a new vhost from default
            when there is no match found

        :returns: ssl vhost associated with name
        :rtype: :class:`~certbot_nginx.obj.VirtualHost`

        """
        vhost = None

        matches = self._get_ranked_matches(target_name)
        vhost = self._select_best_name_match(matches)
        if not vhost:
            if create_if_no_match:
                vhost = self._vhost_from_duplicated_default(target_name)
            else:
                # No matches. Raise a misconfiguration error.
                raise errors.MisconfigurationError(
                            ("Cannot find a VirtualHost matching domain %s. "
                             "In order for Certbot to correctly perform the challenge "
                             "please add a corresponding server_name directive to your "
                             "nginx configuration: "
                             "https://nginx.org/en/docs/http/server_names.html") % (target_name))
        # Note: if we are enhancing with ocsp, vhost should already be ssl.
        if not vhost.ssl:
            self._make_server_ssl(vhost)

        return vhost

    def ipv6_info(self, port):
        """Returns tuple of booleans (ipv6_active, ipv6only_present)
        ipv6_active is true if any server block listens ipv6 address in any port

        ipv6only_present is true if ipv6only=on option exists in any server
        block ipv6 listen directive for the specified port.

        :param str port: Port to check ipv6only=on directive for

        :returns: Tuple containing information if IPv6 is enabled in the global
            configuration, and existence of ipv6only directive for specified port
        :rtype: tuple of type (bool, bool)
        """
        vhosts = self.parser.get_vhosts()
        ipv6_active = False
        ipv6only_present = False
        for vh in vhosts:
            for addr in vh.addrs:
                if addr.ipv6:
                    ipv6_active = True
                if addr.ipv6only and addr.get_port() == port:
                    ipv6only_present = True
        return (ipv6_active, ipv6only_present)

    def _vhost_from_duplicated_default(self, domain):
        if self.new_vhost is None:
            default_vhost = self._get_default_vhost()
            self.new_vhost = self.parser.duplicate_vhost(default_vhost, delete_default=True)
            self.new_vhost.names = set()

        self._add_server_name_to_vhost(self.new_vhost, domain)
        return self.new_vhost

    def _add_server_name_to_vhost(self, vhost, domain):
        vhost.names.add(domain)
        name_block = [['\n    ', 'server_name']]
        for name in vhost.names:
            name_block[0].append(' ')
            name_block[0].append(name)
        self.parser.add_server_directives(vhost, name_block, replace=True)

    def _get_default_vhost(self):
        vhost_list = self.parser.get_vhosts()
        # if one has default_server set, return that one
        default_vhosts = []
        for vhost in vhost_list:
            for addr in vhost.addrs:
                if addr.default:
                    default_vhosts.append(vhost)
                    break

        if len(default_vhosts) == 1:
            return default_vhosts[0]

        # TODO: present a list of vhosts for user to choose from

        raise errors.MisconfigurationError("Could not automatically find a matching server"
            " block. Set the `server_name` directive to use the Nginx installer.")

    def _get_ranked_matches(self, target_name):
        """Returns a ranked list of vhosts that match target_name.
        The ranking gives preference to SSL vhosts.

        :param str target_name: The name to match
        :returns: list of dicts containing the vhost, the matching name, and
            the numerical rank
        :rtype: list

        """
        vhost_list = self.parser.get_vhosts()
        return self._rank_matches_by_name_and_ssl(vhost_list, target_name)

    def _select_best_name_match(self, matches):
        """Returns the best name match of a ranked list of vhosts.

        :param list matches: list of dicts containing the vhost, the matching name,
            and the numerical rank
        :returns: the most matching vhost
        :rtype: :class:`~certbot_nginx.obj.VirtualHost`

        """
        if not matches:
            return None
        elif matches[0]['rank'] in six.moves.range(2, 6):
            # Wildcard match - need to find the longest one
            rank = matches[0]['rank']
            wildcards = [x for x in matches if x['rank'] == rank]
            return max(wildcards, key=lambda x: len(x['name']))['vhost']
        else:
            # Exact or regex match
            return matches[0]['vhost']


    def _rank_matches_by_name_and_ssl(self, vhost_list, target_name):
        """Returns a ranked list of vhosts from vhost_list that match target_name.
        The ranking gives preference to SSL vhosts.

        :param list vhost_list: list of vhosts to filter and rank
        :param str target_name: The name to match
        :returns: list of dicts containing the vhost, the matching name, and
            the numerical rank
        :rtype: list

        """
        # Nginx chooses a matching server name for a request with precedence:
        # 1. exact name match
        # 2. longest wildcard name starting with *
        # 3. longest wildcard name ending with *
        # 4. first matching regex in order of appearance in the file
        matches = []
        for vhost in vhost_list:
            name_type, name = parser.get_best_match(target_name, vhost.names)
            if name_type == 'exact':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 0 if vhost.ssl else 1})
            elif name_type == 'wildcard_start':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 2 if vhost.ssl else 3})
            elif name_type == 'wildcard_end':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 4 if vhost.ssl else 5})
            elif name_type == 'regex':
                matches.append({'vhost': vhost,
                                'name': name,
                                'rank': 6 if vhost.ssl else 7})
        return sorted(matches, key=lambda x: x['rank'])


    def choose_redirect_vhost(self, target_name, port):
        """Chooses a single virtual host for redirect enhancement.

        Chooses the vhost most closely matching target_name that is
        listening to port without using ssl.

        .. todo:: This should maybe return list if no obvious answer
            is presented.

        .. todo:: The special name "$hostname" corresponds to the machine's
            hostname. Currently we just ignore this.

        :param str target_name: domain name
        :param str port: port number
        :returns: vhost associated with name
        :rtype: :class:`~certbot_nginx.obj.VirtualHost`

        """
        matches = self._get_redirect_ranked_matches(target_name, port)
        return self._select_best_name_match(matches)

    def _get_redirect_ranked_matches(self, target_name, port):
        """Gets a ranked list of plaintextish port-listening vhosts matching target_name

        Filter all hosts for those listening on port without using ssl.
        Rank by how well these match target_name.

        :param str target_name: The name to match
        :param str port: port number
        :returns: list of dicts containing the vhost, the matching name, and
            the numerical rank
        :rtype: list

        """
        all_vhosts = self.parser.get_vhosts()
        def _port_matches(test_port, matching_port):
            # test_port is a number, matching is a number or "" or None
            if matching_port == "" or matching_port is None:
                # if no port is specified, Nginx defaults to listening on port 80.
                return test_port == self.DEFAULT_LISTEN_PORT
            else:
                return test_port == matching_port

        def _vhost_matches(vhost, port):
            found_matching_port = False
            if len(vhost.addrs) == 0:
                # if there are no listen directives at all, Nginx defaults to
                # listening on port 80.
                found_matching_port = (port == self.DEFAULT_LISTEN_PORT)
            else:
                for addr in vhost.addrs:
                    if _port_matches(port, addr.get_port()) and addr.ssl == False:
                        found_matching_port = True

            if found_matching_port:
                # make sure we don't have an 'ssl on' directive
                return not self.parser.has_ssl_on_directive(vhost)
            else:
                return False

        matching_vhosts = [vhost for vhost in all_vhosts if _vhost_matches(vhost, port)]

        # We can use this ranking function because sslishness doesn't matter to us, and
        # there shouldn't be conflicting plaintextish servers listening on 80.
        return self._rank_matches_by_name_and_ssl(matching_vhosts, target_name)

    def get_all_names(self):
        """Returns all names found in the Nginx Configuration.

        :returns: All ServerNames, ServerAliases, and reverse DNS entries for
                  virtual host addresses
        :rtype: set

        """
        all_names = set()

        for vhost in self.parser.get_vhosts():
            all_names.update(vhost.names)

            for addr in vhost.addrs:
                host = addr.get_addr()
                if common.hostname_regex.match(host):
                    # If it's a hostname, add it to the names.
                    all_names.add(host)
                elif not common.private_ips_regex.match(host):
                    # If it isn't a private IP, do a reverse DNS lookup
                    try:
                        if addr.ipv6:
                            host = addr.get_ipv6_exploded()
                            socket.inet_pton(socket.AF_INET6, host)
                        else:
                            socket.inet_pton(socket.AF_INET, host)
                        all_names.add(socket.gethostbyaddr(host)[0])
                    except (socket.error, socket.herror, socket.timeout):
                        continue

        return util.get_filtered_names(all_names)

    def _get_snakeoil_paths(self):
        # TODO: generate only once
        tmp_dir = os.path.join(self.config.work_dir, "snakeoil")
        le_key = crypto_util.init_save_key(
            key_size=1024, key_dir=tmp_dir, keyname="key.pem")
        key = OpenSSL.crypto.load_privatekey(
            OpenSSL.crypto.FILETYPE_PEM, le_key.pem)
        cert = acme_crypto_util.gen_ss_cert(key, domains=[socket.gethostname()])
        cert_pem = OpenSSL.crypto.dump_certificate(
            OpenSSL.crypto.FILETYPE_PEM, cert)
        cert_file, cert_path = util.unique_file(
            os.path.join(tmp_dir, "cert.pem"), mode="wb")
        with cert_file:
            cert_file.write(cert_pem)
        return cert_path, le_key.file

    def _make_server_ssl(self, vhost):
        """Make a server SSL.

        Make a server SSL by adding new listen and SSL directives.

        :param vhost: The vhost to add SSL to.
        :type vhost: :class:`~certbot_nginx.obj.VirtualHost`

        """
        ipv6info = self.ipv6_info(self.config.tls_sni_01_port)
        ipv6_block = ['']
        ipv4_block = ['']

        # If the vhost was implicitly listening on the default Nginx port,
        # have it continue to do so.
        if len(vhost.addrs) == 0:
            listen_block = [['\n    ', 'listen', ' ', self.DEFAULT_LISTEN_PORT]]
            self.parser.add_server_directives(vhost, listen_block, replace=False)

        if vhost.ipv6_enabled():
            ipv6_block = ['\n    ',
                          'listen',
                          ' ',
                          '[::]:{0}'.format(self.config.tls_sni_01_port),
                          ' ',
                          'ssl']
            if not ipv6info[1]:
                # ipv6only=on is absent in global config
                ipv6_block.append(' ')
                ipv6_block.append('ipv6only=on')

        if vhost.ipv4_enabled():
            ipv4_block = ['\n    ',
                          'listen',
                          ' ',
                          '{0}'.format(self.config.tls_sni_01_port),
                          ' ',
                          'ssl']

        snakeoil_cert, snakeoil_key = self._get_snakeoil_paths()

        ssl_block = ([
            ipv6_block,
            ipv4_block,
            ['\n    ', 'ssl_certificate', ' ', snakeoil_cert],
            ['\n    ', 'ssl_certificate_key', ' ', snakeoil_key],
            ['\n    ', 'include', ' ', self.mod_ssl_conf],
            ['\n    ', 'ssl_dhparam', ' ', self.ssl_dhparams],
        ])

        self.parser.add_server_directives(
            vhost, ssl_block, replace=False)

    ##################################
    # enhancement methods (IInstaller)
    ##################################
    def supported_enhancements(self):  # pylint: disable=no-self-use
        """Returns currently supported enhancements."""
        return ['redirect', 'staple-ocsp']

    def enhance(self, domain, enhancement, options=None):
        """Enhance configuration.

        :param str domain: domain to enhance
        :param str enhancement: enhancement type defined in
            :const:`~certbot.constants.ENHANCEMENTS`
        :param options: options for the enhancement
            See :const:`~certbot.constants.ENHANCEMENTS`
            documentation for appropriate parameter.

        """
        try:
            return self._enhance_func[enhancement](domain, options)
        except (KeyError, ValueError):
            raise errors.PluginError(
                "Unsupported enhancement: {0}".format(enhancement))
        except errors.PluginError:
            logger.warning("Failed %s for %s", enhancement, domain)
            raise

    def _has_certbot_redirect(self, vhost):
        test_redirect_block = _test_block_from_block(REDIRECT_BLOCK)
        return vhost.contains_list(test_redirect_block)

    def _has_certbot_redirect_comment(self, vhost):
        test_redirect_comment_block = _test_block_from_block(REDIRECT_COMMENT_BLOCK)
        return vhost.contains_list(test_redirect_comment_block)

    def _add_redirect_block(self, vhost, active=True):
        """Add redirect directive to vhost
        """
        if active:
            redirect_block = REDIRECT_BLOCK
        else:
            redirect_block = REDIRECT_COMMENT_BLOCK

        self.parser.add_server_directives(
            vhost, redirect_block, replace=False)

    def _enable_redirect(self, domain, unused_options):
        """Redirect all equivalent HTTP traffic to ssl_vhost.

        If the vhost is listening plaintextishly, separate out the
        relevant directives into a new server block and add a rewrite directive.

        .. note:: This function saves the configuration

        :param str domain: domain to enable redirect for
        :param unused_options: Not currently used
        :type unused_options: Not Available
        """

        port = self.DEFAULT_LISTEN_PORT
        vhost = None
        # If there are blocks listening plaintextishly on self.DEFAULT_LISTEN_PORT,
        # choose the most name-matching one.

        vhost = self.choose_redirect_vhost(domain, port)

        if vhost is None:
            logger.info("No matching insecure server blocks listening on port %s found.",
                self.DEFAULT_LISTEN_PORT)
            return

        if vhost.ssl:
            new_vhost = self.parser.duplicate_vhost(vhost,
                only_directives=['listen', 'server_name'])

            def _ssl_match_func(directive):
                return 'ssl' in directive

            def _no_ssl_match_func(directive):
                return 'ssl' not in directive

            # remove all ssl addresses from the new block
            self.parser.remove_server_directives(new_vhost, 'listen', match_func=_ssl_match_func)

            # remove all non-ssl addresses from the existing block
            self.parser.remove_server_directives(vhost, 'listen', match_func=_no_ssl_match_func)

            vhost = new_vhost

        if self._has_certbot_redirect(vhost):
            logger.info("Traffic on port %s already redirecting to ssl in %s",
                self.DEFAULT_LISTEN_PORT, vhost.filep)
        elif vhost.has_redirect():
            if not self._has_certbot_redirect_comment(vhost):
                self._add_redirect_block(vhost, active=False)
            logger.info("The appropriate server block is already redirecting "
                        "traffic. To enable redirect anyway, uncomment the "
                        "redirect lines in %s.", vhost.filep)
        else:
            # Redirect plaintextish host to https
            self._add_redirect_block(vhost, active=True)
            logger.info("Redirecting all traffic on port %s to ssl in %s",
                self.DEFAULT_LISTEN_PORT, vhost.filep)

    def _enable_ocsp_stapling(self, domain, chain_path):
        """Include OCSP response in TLS handshake

        :param str domain: domain to enable OCSP response for
        :param chain_path: chain file path
        :type chain_path: `str` or `None`

        """
        vhost = self.choose_vhost(domain)
        if self.version < (1, 3, 7):
            raise errors.PluginError("Version 1.3.7 or greater of nginx "
                                     "is needed to enable OCSP stapling")

        if chain_path is None:
            raise errors.PluginError(
                "--chain-path is required to enable "
                "Online Certificate Status Protocol (OCSP) stapling "
                "on nginx >= 1.3.7.")

        stapling_directives = [
            ['\n    ', 'ssl_trusted_certificate', ' ', chain_path],
            ['\n    ', 'ssl_stapling', ' ', 'on'],
            ['\n    ', 'ssl_stapling_verify', ' ', 'on'], ['\n']]

        try:
            self.parser.add_server_directives(vhost,
                                              stapling_directives, replace=False)
        except errors.MisconfigurationError as error:
            logger.debug(error)
            raise errors.PluginError("An error occurred while enabling OCSP "
                                     "stapling for {0}.".format(vhost.names))

        self.save_notes += ("OCSP Stapling was enabled "
                            "on SSL Vhost: {0}.\n".format(vhost.filep))
        self.save_notes += "\tssl_trusted_certificate {0}\n".format(chain_path)
        self.save_notes += "\tssl_stapling on\n"
        self.save_notes += "\tssl_stapling_verify on\n"

    ######################################
    # Nginx server management (IInstaller)
    ######################################
    def restart(self):
        """Restarts nginx server.

        :raises .errors.MisconfigurationError: If either the reload fails.

        """
        nginx_restart(self.conf('ctl'), self.nginx_conf)

    def config_test(self):  # pylint: disable=no-self-use
        """Check the configuration of Nginx for errors.

        :raises .errors.MisconfigurationError: If config_test fails

        """
        try:
            util.run_script([self.conf('ctl'), "-c", self.nginx_conf, "-t"])
        except errors.SubprocessError as err:
            raise errors.MisconfigurationError(str(err))

    def _verify_setup(self):
        """Verify the setup to ensure safe operating environment.

        Make sure that files/directories are setup with appropriate permissions
        Aim for defensive coding... make sure all input files
        have permissions of root.

        """
        uid = os.geteuid()
        util.make_or_verify_dir(
            self.config.work_dir, core_constants.CONFIG_DIRS_MODE, uid)
        util.make_or_verify_dir(
            self.config.backup_dir, core_constants.CONFIG_DIRS_MODE, uid)
        util.make_or_verify_dir(
            self.config.config_dir, core_constants.CONFIG_DIRS_MODE, uid)

    def get_version(self):
        """Return version of Nginx Server.

        Version is returned as tuple. (ie. 2.4.7 = (2, 4, 7))

        :returns: version
        :rtype: tuple

        :raises .PluginError:
            Unable to find Nginx version or version is unsupported

        """
        try:
            proc = subprocess.Popen(
                [self.conf('ctl'), "-c", self.nginx_conf, "-V"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True)
            text = proc.communicate()[1]  # nginx prints output to stderr
        except (OSError, ValueError) as error:
            logger.debug(error, exc_info=True)
            raise errors.PluginError(
                "Unable to run %s -V" % self.conf('ctl'))

        version_regex = re.compile(r"nginx/([0-9\.]*)", re.IGNORECASE)
        version_matches = version_regex.findall(text)

        sni_regex = re.compile(r"TLS SNI support enabled", re.IGNORECASE)
        sni_matches = sni_regex.findall(text)

        ssl_regex = re.compile(r" --with-http_ssl_module")
        ssl_matches = ssl_regex.findall(text)

        if not version_matches:
            raise errors.PluginError("Unable to find Nginx version")
        if not ssl_matches:
            raise errors.PluginError(
                "Nginx build is missing SSL module (--with-http_ssl_module).")
        if not sni_matches:
            raise errors.PluginError("Nginx build doesn't support SNI")

        nginx_version = tuple([int(i) for i in version_matches[0].split(".")])

        # nginx < 0.8.48 uses machine hostname as default server_name instead of
        # the empty string
        if nginx_version < (0, 8, 48):
            raise errors.NotSupportedError("Nginx version must be 0.8.48+")

        return nginx_version

    def more_info(self):
        """Human-readable string to help understand the module"""
        return (
            "Configures Nginx to authenticate and install HTTPS.{0}"
            "Server root: {root}{0}"
            "Version: {version}".format(
                os.linesep, root=self.parser.config_root,
                version=".".join(str(i) for i in self.version))
        )

    ###################################################
    # Wrapper functions for Reverter class (IInstaller)
    ###################################################
    def save(self, title=None, temporary=False):
        """Saves all changes to the configuration files.

        :param str title: The title of the save. If a title is given, the
            configuration will be saved as a new checkpoint and put in a
            timestamped directory.

        :param bool temporary: Indicates whether the changes made will
            be quickly reversed in the future (ie. challenges)

        :raises .errors.PluginError: If there was an error in
            an attempt to save the configuration, or an error creating a
            checkpoint

        """
        save_files = set(self.parser.parsed.keys())
        self.add_to_checkpoint(save_files, self.save_notes, temporary)
        self.save_notes = ""

        # Change 'ext' to something else to not override existing conf files
        self.parser.filedump(ext='')
        if title and not temporary:
            self.finalize_checkpoint(title)

    def recovery_routine(self):
        """Revert all previously modified files.

        Reverts all modified files that have not been saved as a checkpoint

        :raises .errors.PluginError: If unable to recover the configuration

        """
        super(NginxConfigurator, self).recovery_routine()
        self.new_vhost = None
        self.parser.load()

    def revert_challenge_config(self):
        """Used to cleanup challenge configurations.

        :raises .errors.PluginError: If unable to revert the challenge config.

        """
        self.revert_temporary_config()
        self.new_vhost = None
        self.parser.load()

    def rollback_checkpoints(self, rollback=1):
        """Rollback saved checkpoints.

        :param int rollback: Number of checkpoints to revert

        :raises .errors.PluginError: If there is a problem with the input or
            the function is unable to correctly revert the configuration

        """
        super(NginxConfigurator, self).rollback_checkpoints(rollback)
        self.new_vhost = None
        self.parser.load()

    ###########################################################################
    # Challenges Section for IAuthenticator
    ###########################################################################
    def get_chall_pref(self, unused_domain):  # pylint: disable=no-self-use
        """Return list of challenge preferences."""
        return [challenges.TLSSNI01]

    # Entry point in main.py for performing challenges
    def perform(self, achalls):
        """Perform the configuration related challenge.

        This function currently assumes all challenges will be fulfilled.
        If this turns out not to be the case in the future. Cleanup and
        outstanding challenges will have to be designed better.

        """
        self._chall_out += len(achalls)
        responses = [None] * len(achalls)
        chall_doer = tls_sni_01.NginxTlsSni01(self)

        for i, achall in enumerate(achalls):
            # Currently also have chall_doer hold associated index of the
            # challenge. This helps to put all of the responses back together
            # when they are all complete.
            chall_doer.add_chall(achall, i)

        sni_response = chall_doer.perform()
        # Must restart in order to activate the challenges.
        # Handled here because we may be able to load up other challenge types
        self.restart()

        # Go through all of the challenges and assign them to the proper place
        # in the responses return value. All responses must be in the same order
        # as the original challenges.
        for i, resp in enumerate(sni_response):
            responses[chall_doer.indices[i]] = resp

        return responses

    # called after challenges are performed
    def cleanup(self, achalls):
        """Revert all challenges."""
        self._chall_out -= len(achalls)

        # If all of the challenges have been finished, clean up everything
        if self._chall_out <= 0:
            self.revert_challenge_config()
            self.restart()


def _test_block_from_block(block):
    test_block = nginxparser.UnspacedList(block)
    parser.comment_directive(test_block, 0)
    return test_block[:-1]

def nginx_restart(nginx_ctl, nginx_conf):
    """Restarts the Nginx Server.

    .. todo:: Nginx restart is fatal if the configuration references
        non-existent SSL cert/key files. Remove references to /etc/letsencrypt
        before restart.

    :param str nginx_ctl: Path to the Nginx binary.

    """
    try:
        proc = subprocess.Popen([nginx_ctl, "-c", nginx_conf, "-s", "reload"])
        proc.communicate()

        if proc.returncode != 0:
            # Maybe Nginx isn't running
            # Write to temporary files instead of piping because of communication issues on Arch
            # https://github.com/certbot/certbot/issues/4324
            with tempfile.TemporaryFile() as out:
                with tempfile.TemporaryFile() as err:
                    nginx_proc = subprocess.Popen([nginx_ctl, "-c", nginx_conf],
                        stdout=out, stderr=err)
                    nginx_proc.communicate()
                    if nginx_proc.returncode != 0:
                        # Enter recovery routine...
                        raise errors.MisconfigurationError(
                            "nginx restart failed:\n%s\n%s" % (out.read(), err.read()))

    except (OSError, ValueError):
        raise errors.MisconfigurationError("nginx restart failed")
    # Nginx can take a moment to recognize a newly added TLS SNI servername, so sleep
    # for a second. TODO: Check for expected servername and loop until it
    # appears or return an error if looping too long.
    time.sleep(1)


def install_ssl_options_conf(options_ssl, options_ssl_digest):
    """Copy Certbot's SSL options file into the system's config dir if required."""
    return common.install_version_controlled_file(options_ssl, options_ssl_digest,
        constants.MOD_SSL_CONF_SRC, constants.ALL_SSL_OPTIONS_HASHES)
