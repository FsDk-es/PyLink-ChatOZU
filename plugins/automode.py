"""
automode.py - Provide simple channel ACL management by giving prefix modes to users matching
hostmasks or exttargets.
"""
import collections
import string

from pylinkirc import conf, structures, utils, world
from pylinkirc.coremods import permissions
from pylinkirc.log import log

mydesc = ("The \x02Automode\x02 plugin provides simple channel ACL management by giving prefix modes "
          "to users matching hostmasks or exttargets.")

# Register ourselves as a service.
modebot = utils.register_service("automode", default_nick="Automode", desc=mydesc)
reply = modebot.reply
error = modebot.error

# Databasing variables.
dbname = conf.get_database_name('automode')
datastore = structures.JSONDataStore('automode', dbname, default_db=collections.defaultdict(dict))

db = datastore.store

# The default set of Automode permissions.
default_permissions = {"$ircop": ['automode.manage.relay_owned', 'automode.sync.relay_owned',
                                  'automode.list']}

def _join_db_channels(irc):
    """
    Joins the Automode service client to channels on the current network in its DB.
    """
    if not irc.connected.is_set():
        log.debug('(%s) _join_db_channels: aborting, network not ready yet', irc.name)
        return

    for entry in db:
        netname, channel = entry.split('#', 1)
        channel = '#' + channel
        if netname == irc.name:
            modebot.add_persistent_channel(irc, 'automode', channel)

def main(irc=None):
    """Main function, called during plugin loading."""

    # Load the automode database.
    datastore.load()

    # Register our permissions.
    permissions.add_default_permissions(default_permissions)

    if irc:  # This was a reload.
        for ircobj in world.networkobjects.values():
            _join_db_channels(ircobj)

def die(irc=None):
    """Saves the Automode database and quit."""
    datastore.die()
    permissions.remove_default_permissions(default_permissions)
    utils.unregister_service('automode')

def _check_automode_access(irc, uid, channel, command):
    """Checks the caller's access to Automode."""
    # Automode defines the following permissions, where <command> is either "manage", "list",
    # "sync", "clear", "remotemanage", "remotelist", "remotesync", "remoteclear":
    # - automode.<command> OR automode.<command>.*: ability to <command> automode on all channels.
    # - automode.<command>.relay_owned: ability to <command> automode on channels owned via Relay.
    #   If Relay isn't loaded, this permission check FAILS.
    # - automode.<command>.#channel: ability to <command> automode on the given channel.
    # - automode.savedb: ability to save the automode DB.
    log.debug('(%s) Automode: checking access for %s/%s for %s capability on %s', irc.name, uid,
              irc.get_hostmask(uid), command, channel)

    baseperm = 'automode.%s' % command
    try:
        # First, check the catch all and channel permissions.
        perms = [baseperm, baseperm+'.*', '%s.%s' % (baseperm, channel)]
        return permissions.check_permissions(irc, uid, perms)
    except utils.NotAuthorizedError:
        if not command.startswith('remote'):
            # Relay-based ACL checking only works with local calls.
            log.debug('(%s) Automode: falling back to automode.%s.relay_owned', irc.name, command)
            permissions.check_permissions(irc, uid, [baseperm+'.relay_owned'], also_show=perms)

            relay = world.plugins.get('relay')
            if relay is None:
                raise utils.NotAuthorizedError("You are not authorized to use Automode when Relay is "
                                               "disabled. You are missing one of the following "
                                               "permissions: %s or %s.%s" % (baseperm, baseperm, channel))
            elif (irc.name, channel) not in relay.db:
                raise utils.NotAuthorizedError("The network you are on does not own the relay channel %s." % channel)
            return True
        raise

def match(irc, channel, uids=None):
    """
    Set modes on matching users. If uids is not given, check all users in the channel and give
    them modes as needed.
    """
    if isinstance(channel, int) or str(channel).startswith(tuple(string.digits)):
        channel = '#' + str(channel)  # Mangle channels on networks where they're stored as an ID
    dbentry = db.get(irc.name+channel)
    if not irc.has_cap('has-irc-modes'):
        log.debug('(%s) automode: skipping match() because IRC modes are not supported on this protocol', irc.name)
        return
    elif dbentry is None:
        return

    modebot_uid = modebot.uids.get(irc.name)

    # Check every mask defined in the channel ACL.
    outgoing_modes = []

    # If UIDs are given, match those. Otherwise, match all users in the given channel.
    uids = uids or irc.channels[channel].users

    for mask, modes in dbentry.items():
        for uid in uids:
            if irc.match_host(mask, uid):
                # User matched a mask. Filter the mode list given to only those that are valid
                # prefix mode characters.
                outgoing_modes += [('+'+mode, uid) for mode in modes if mode in irc.prefixmodes]
                log.debug("(%s) automode: Filtered mode list of %s to %s (protocol:%s)",
                          irc.name, modes, outgoing_modes, irc.protoname)

    if outgoing_modes:
        # If the Automode bot is missing, send the mode through the PyLink server.
        if modebot_uid not in irc.users:
            modebot_uid = irc.sid

        log.debug("(%s) automode: sending modes from modebot_uid %s",
                  irc.name, modebot_uid)

        irc.mode(modebot_uid, channel, outgoing_modes)

        # Create a hook payload to support plugins like relay.
        irc.call_hooks([modebot_uid, 'AUTOMODE_MODE',
                      {'target': channel, 'modes': outgoing_modes, 'parse_as': 'MODE'}])

def handle_endburst(irc, source, command, args):
    """ENDBURST hook handler - used to join the Automode service to channels where it has entries."""
    if source == irc.uplink:
        _join_db_channels(irc)
utils.add_hook(handle_endburst, 'ENDBURST')

def handle_join(irc, source, command, args):
    """
    Automode JOIN listener. This sets modes accordingly if the person joining matches a mask in the
    ACL.
    """
    channel = irc.to_lower(args['channel'])
    match(irc, channel, args['users'])

utils.add_hook(handle_join, 'JOIN')
utils.add_hook(handle_join, 'PYLINK_RELAY_JOIN')  # Handle the relay version of join
utils.add_hook(handle_join, 'PYLINK_SERVICE_JOIN')  # And the version for service bots

def handle_services_login(irc, source, command, args):
    """
    Handles services login change, to trigger Automode matching.
    """
    for channel in irc.users[source].channels:
        # Look at all the users' channels for any possible changes.
        match(irc, channel, [source])

utils.add_hook(handle_services_login, 'CLIENT_SERVICES_LOGIN')
utils.add_hook(handle_services_login, 'PYLINK_RELAY_SERVICES_LOGIN')

def _get_channel_pair(irc, source, chanpair, perm=None):
    """
    Fetches the network and channel given a channel pair, also optionally checking the caller's permissions.
    """
    log.debug('(%s) Looking up chanpair %s', irc.name, chanpair)

    if '#' not in chanpair and chanpair.startswith(tuple(string.digits)):
        chanpair = '#' + chanpair  # Mangle channels on networks where they're stored by ID

    try:
        network, channel = chanpair.split('#', 1)
    except ValueError:
        raise ValueError("Invalid channel pair %r" % chanpair)
    channel = '#' + channel
    channel = irc.to_lower(channel)

    if network:
        ircobj = world.networkobjects.get(network)
    else:
        ircobj = irc

    if not ircobj:
        raise ValueError("Unknown network %s" % network)

    if perm is not None:
        # Only check for permissions if we're told to and the irc object exists.
        if ircobj.name != irc.name:
            perm = 'remote' + perm

        _check_automode_access(irc, source, channel, perm)

    return (ircobj, channel)

def setacc(irc, source, args):
    """<channel/chanpair> <mask> <mode list>

    Assigns the given prefix mode characters to the given mask for the channel given. Extended targets are supported for masks - use this to your advantage!

    Channel pairs are also supported (for operations on remote channels), using the form "network#channel".


    Examples:

    \x02SETACC #channel *!*@localhost ohv

    \x02SETACC #channel $account v

    \x02SETACC othernet#channel $ircop:Network?Administrator qo

    \x02SETACC #staffchan $channel:#mainchan:op o
    """
    if not irc.has_cap('has-irc-modes'):
        error(irc, "IRC style modes are not supported on this protocol.")
        return

    try:
        chanpair, mask, modes = args
    except ValueError:
        error(irc, "Invalid arguments given. Needs 3: channel, mask, mode list.")
        return
    else:
        ircobj, channel = _get_channel_pair(irc, source, chanpair, perm='manage')

    # Database entries for any network+channel pair are automatically created using
    # defaultdict. Note: string keys are used here instead of tuples so they can be
    # exported easily as JSON.
    dbentry = db[ircobj.name+channel]

    modes = modes.lstrip('+')  # remove extraneous leading +'s
    dbentry[mask] = modes
    log.info('(%s) %s set modes +%s for %s on %s', ircobj.name, irc.get_hostmask(source), modes, mask, channel)
    reply(irc, "Done. \x02%s\x02 now has modes \x02+%s\x02 in \x02%s\x02." % (mask, modes, channel))

    # Join the Automode bot to the channel persistently.
    modebot.add_persistent_channel(ircobj, 'automode', channel)

modebot.add_cmd(setacc, aliases=('setaccess', 'set'), featured=True)

def delacc(irc, source, args):
    """<channel/chanpair> <mask or range string>

    Removes the Automode entry for the given mask or range string, if they exist.

    Range strings are indices (entry numbers) or ranges of them joined together with commas: e.g.
    "1", "2-10", "1,3,5-8". Entry numbers are shown by LISTACC.
    """
    try:
        chanpair, mask = args
    except ValueError:
        error(irc, "Invalid arguments given. Needs 2: channel, mask")
        return
    else:
        ircobj, channel = _get_channel_pair(irc, source, chanpair, perm='manage')

    dbentry = db.get(ircobj.name+channel)

    if dbentry is None:
        error(irc, "No Automode access entries exist for \x02%s\x02." % channel)
        return

    if mask in dbentry:
        del dbentry[mask]
        log.info('(%s) %s removed modes for %s on %s', ircobj.name, irc.get_hostmask(source), mask, channel)
        reply(irc, "Done. Removed the Automode access entry for \x02%s\x02 in \x02%s\x02." % (mask, channel))
    else:
        # Treat the mask as a range string.
        try:
            new_keys = utils.remove_range(mask, sorted(dbentry.keys()))
        except ValueError:
            error(irc, "No Automode access entry for \x02%s\x02 exists in \x02%s\x02." % (mask, channel))
            return

        # XXX: Automode entries are actually unordered: what we're actually doing is sorting the keys
        # by name into a list, running remove_range on that, and removing the difference.
        removed = []
        source_host = irc.get_hostmask(source)
        for mask_entry in dbentry.copy():
            if mask_entry not in new_keys:
                del dbentry[mask_entry]
                log.info('(%s) %s removed modes for %s on %s', ircobj.name, source_host, mask_entry, channel)
                removed.append(mask_entry)

        reply(irc, 'Done. Removed \x02%d\x02 entries on \x02%s\x02: %s' % (len(removed), channel, ', '.join(removed)))

    # Remove channels if no more entries are left.
    if not dbentry:
        log.debug("Automode: purging empty channel pair %s/%s", ircobj.name, channel)
        del db[ircobj.name+channel]
        modebot.remove_persistent_channel(ircobj, 'automode', channel)

modebot.add_cmd(delacc, aliases=('delaccess', 'del'), featured=True)

def listacc(irc, source, args):
    """<channel/chanpair>

    Lists all Automode entries for the given channel."""
    try:
        chanpair = args[0]
    except IndexError:
        error(irc, "Invalid arguments given. Needs 1: channel.")
        return
    else:
        ircobj, channel = _get_channel_pair(irc, source, chanpair, perm='list')

    dbentry = db.get(ircobj.name+channel)
    if not dbentry:
        error(irc, "No Automode access entries exist for \x02%s\x02." % channel)
        return

    else:
        # Iterate over all entries and print them. Do this in private to prevent channel
        # floods.
        reply(irc, "Showing Automode entries for \x02%s\x02:" % channel, private=True)
        for entrynum, entry in enumerate(sorted(dbentry.items()), start=1):
            mask, modes = entry
            reply(irc, "[%s] \x02%s\x02 has modes +\x02%s\x02" % (entrynum, mask, modes), private=True)
        reply(irc, "End of Automode entries list.", private=True)

modebot.add_cmd(listacc, featured=True, aliases=('listaccess',))

def save(irc, source, args):
    """takes no arguments.

    Saves the Automode database to disk."""
    permissions.check_permissions(irc, source, ['automode.savedb'])
    datastore.save()
    reply(irc, 'Done.')

modebot.add_cmd(save)

def syncacc(irc, source, args):
    """<channel/chanpair>

    Syncs Automode access lists to the channel.
    """
    try:
        chanpair = args[0]
    except IndexError:
        error(irc, "Invalid arguments given. Needs 1: channel.")
        return
    else:
        ircobj, channel = _get_channel_pair(irc, source, chanpair, perm='sync')

    log.info('(%s) %s synced modes on %s', ircobj.name, irc.get_hostmask(source), channel)
    match(ircobj, channel)

    reply(irc, 'Done.')

modebot.add_cmd(syncacc, featured=True, aliases=('sync', 'syncaccess'))

def clearacc(irc, source, args):
    """<channel>

    Removes all Automode entries for the given channel.
    """

    try:
        chanpair = args[0]
    except IndexError:
        error(irc, "Invalid arguments given. Needs 1: channel.")
        return
    else:
        ircobj, channel = _get_channel_pair(irc, source, chanpair, perm='clear')

    if db.get(ircobj.name+channel):
        del db[ircobj.name+channel]
        log.info('(%s) %s cleared modes on %s', ircobj.name, irc.get_hostmask(source), channel)
        reply(irc, "Done. Removed all Automode access entries for \x02%s\x02." % channel)
        modebot.remove_persistent_channel(ircobj, 'automode', channel)
    else:
        error(irc, "No Automode access entries exist for \x02%s\x02." % channel)

modebot.add_cmd(clearacc, aliases=('clearaccess', 'clear'), featured=True)
