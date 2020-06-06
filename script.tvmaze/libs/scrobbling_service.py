# coding: utf-8
# (c) Roman Miroshnychenko <roman1972@gmail.com> 2020
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Scraper actions"""
# pylint: disable=missing-docstring
from __future__ import absolute_import, division, unicode_literals

import os
import re
import time
import uuid
from collections import defaultdict, namedtuple
from pprint import pformat

import pyqrcode
import six
from kodi_six import xbmc

from .gui import DIALOG, ConfirmationDialog, background_progress_dialog
from .kodi_service import ADDON, ADDON_ID, PROFILE_DIR, ICON, GETTEXT, logger
from .medialibrary_api import (NoDataError, get_tvshows, get_episodes, get_tvshow_details,
                               get_episode_details, get_recent_episodes, set_show_uniqueid)
from .tvmaze_api import (AuthorizationError, UpdateError, GetInfoError, start_authorization,
                         send_episodes, is_authorized, get_show_info_by_external_id)

try:
    # pylint: disable=unused-import
    from typing import Text, Dict, Any, List, Tuple, Callable, Optional, Union
except ImportError:
    pass

_ = GETTEXT

SUPPORTED_IDS = ('tvmaze', 'tvdb', 'imdb')

UniqueId = namedtuple('UniqueId', ['show_id', 'provider'])  # pylint: disable=invalid-name


class StatusType(object):  # pylint: disable=too-few-public-methods
    WATCHED = 0
    ACQUIRED = 1
    SKIPPED = 2


def authorize_addon():
    # type: () -> None
    """
    Authorize the addon on TVmaze

    The function sends authorization request to TVmaze and saves TVmaze
    username and API token for scrobbling requests authorization
    """
    if is_authorized():
        answer = DIALOG.yesno(
            _('TVmaze Scrobbler'),
            _('The addon is already authorized.[CR]Authorize again?')
        )
        if not answer:
            return
    keyboard = xbmc.Keyboard()
    keyboard.setHeading(_('Your TVmaze account email'))
    keyboard.doModal()
    if keyboard.isConfirmed():
        email = keyboard.getText()
        if re.search(r'^[\w.\-+]+@[\w.-]+\.[\w]+$', email) is None:
            logger.error('Invalid email: {}'.format(email))
            DIALOG.notification(ADDON_ID, _('Invalid email'), icon='error', time=3000)
            return
        try:
            token, confirm_url = start_authorization(email)
        except AuthorizationError as exc:
            logger.error('TVmaze authorization error: {}'.format(exc))
            message = _('Authorization error: {}').format(exc)
            DIALOG.notification(ADDON_ID, message, icon='error')
            return
        qrcode_filename = uuid.uuid4().hex + '.png'
        qrcode_path = os.path.join(PROFILE_DIR, qrcode_filename)
        qrcode_image = pyqrcode.create(confirm_url)
        qrcode_image.png(qrcode_path, scale=10)
        confirmation_dialog = ConfirmationDialog(email, token, confirm_url, qrcode_path)
        confirmation_dialog.doModal()
        if confirmation_dialog.is_confirmed:
            ADDON.setSettingString('username', confirmation_dialog.username)
            ADDON.setSettingString('apikey', confirmation_dialog.apikey)
            DIALOG.notification(ADDON_ID, _('Addon has been authorized successfully'),
                                icon=ICON, sound=False, time=3000)
        elif confirmation_dialog.error_message is not None:
            logger.error('Confirmation error: {}'.format(confirmation_dialog.error_message))
            message = _('Confirmation error: {}').format(confirmation_dialog.error_message)
            DIALOG.notification(ADDON_ID, message, icon='error')
        del confirmation_dialog

def _get_unique_id(uniqueid_dict):
    # type: (Dict[Text, Text]) -> Optional[UniqueId]
    """
    Get a show ID in one of the supported online databases

    :param uniqueid_dict: uniqueid dict from Kodi JSON-RPC API
    :return: a named tuple of unique ID and online data provider
    """
    for provider in SUPPORTED_IDS:
        show_id = uniqueid_dict.get(provider)
        if show_id is not None:
            if provider == 'tvdb':
                provider = 'thetvdb'
            return UniqueId(show_id, provider)
    return None


def _prepare_episode_list(kodi_episode_list):
    # type: (List[Dict[Text, Any]]) -> List[Dict[Text, int]]
    episodes_for_tvmaze = []
    for episode in kodi_episode_list:
        if episode['season']:  # Todo: add support for specials
            episodes_for_tvmaze.append({
                'season': episode['season'],
                'episode': episode['episode'],
                'marked_at': int(time.time()),
                'type': StatusType.WATCHED if episode['playcount'] else StatusType.ACQUIRED,
            })
    return episodes_for_tvmaze


def _send_show_episodes_to_tvmaze(tvmaze_id, episodes):
    # type: (Union[int, Text], List[Dict[Text, int]]) -> None
    """
    Send episodes statuses of a TV show to TVmaze

    :param tvmaze_id: show ID on TVmaze
    :param episodes: the list of episodes
    :raises UpdateError: on update error
    """
    episodes_for_tvmaze = _prepare_episode_list(episodes)
    send_episodes(episodes_for_tvmaze, tvmaze_id)


def _get_tvmaze_id(kodi_show_info):
    # type: (Dict[Text, Any]) -> Optional[int]
    uniqueid_dict = kodi_show_info['uniqueid']
    unique_id = _get_unique_id(uniqueid_dict)
    if unique_id is None:
        return None
    if unique_id.provider == 'tvmaze':
        return int(unique_id.show_id)
    try:
        show_info = get_show_info_by_external_id(unique_id.show_id, unique_id.provider)
    except GetInfoError:
        return None
    tvmaze_id = show_info['id']
    set_show_uniqueid(kodi_show_info['tvshowid'], tvmaze_id)
    return tvmaze_id


def update_all_episodes():
    # type: () -> None
    """
    Fetch the list of all episodes from medialibrary and send watched statuses to TVmaze
    """
    if not is_authorized():
        logger.warning('Addon is not authorized')
        return
    logger.info('Syncing all episodes info to TVmaze...')
    errors = False
    with background_progress_dialog(_('TVmaze Scrobbler'), _('Updating episodes')) as dialog:
        try:
            tv_shows = get_tvshows()
        except NoDataError:
            logger.warning('Medialibrary has no TV shows')
            return
        logger.debug('TV shows from Kodi:\n{}'.format(pformat(tv_shows)))
        shows_count = len(tv_shows)
        for n, show in enumerate(tv_shows, 1):
            percent = int(100 * n / shows_count)
            message = _(r'Updating episodes for show \"{show_name}\": {count}/{total}').format(
                show_name=show['label'],
                count=n,
                total=shows_count
            )
            dialog.update(percent, _('TVmaze Scrobbler'), message)
            tvmaze_id = _get_tvmaze_id(show)
            if tvmaze_id is None:
                logger.error(
                    'Unable to determine TVmaze id from show info: {}'.format(pformat(show)))
                continue
            try:
                episodes = get_episodes(show['tvshowid'])
            except NoDataError:
                logger.warning('TV show "{}" has no episodes'.format(show['label']))
                continue
            logger.debug('"{}" episodes from Kodi:\n{}'.format(show['label'], pformat(episodes)))
            try:
                _send_show_episodes_to_tvmaze(tvmaze_id, episodes)
            except UpdateError as exc:
                errors = True
                logger.error(
                    'Unable to update episodes for show "{}": {}'.format(show['label'], exc))
                continue
    if errors:
        DIALOG.notification(ADDON_ID, _('Update completed with errors'), icon='error')
    else:
        DIALOG.notification(ADDON_ID, _('Update completed'), icon=ICON, time=3000, sound=False)


def update_single_episode(episode_id):
    # type: (int) -> None
    """Update watched status for a single episode"""
    if not is_authorized():
        return
    episode_info = get_episode_details(episode_id)
    tvshow_info = get_tvshow_details(episode_info['tvshowid'])
    tvmaze_id = _get_tvmaze_id(tvshow_info)
    if tvmaze_id is None:
        logger.error(
            'Unable to determine TVmaze id from show info: {}'.format(pformat(tvshow_info)))
        return
    episodes_for_tvmaze = _prepare_episode_list([episode_info])
    try:
        send_episodes(episodes_for_tvmaze, tvmaze_id)
    except UpdateError as exc:
        logger.error('Failed to update episode status:\n{}Error: {}'.format(episode_info, exc))
        DIALOG.notification(ADDON_ID, _('Failed to update episode status'), icon='error')
    else:
        DIALOG.notification(
            ADDON_ID, _('Episode status updated'), icon=ICON, time=3000, sound=False)


def update_recent_episodes():
    # type: () -> None
    """Add recent episodes to TVmaze"""
    if not is_authorized():
        return
    errors = False
    try:
        recent_episodes = get_recent_episodes()
    except NoDataError:
        return
    logger.debug('Recent episodes from Kodi:\n{}'.format(pformat(recent_episodes)))
    id_mapping = {}
    episode_mapping = defaultdict(list)
    for episode in recent_episodes:
        if episode['tvshowid'] not in id_mapping:
            show_info = get_tvshow_details(episode['tvshowid'])
            tvmaze_id = _get_tvmaze_id(show_info)
            if tvmaze_id is None:
                logger.error(
                    'Unable to determine TVmaze id from show info: {}'.format(
                        pformat(show_info)))
                continue
            id_mapping[episode['tvshowid']] = tvmaze_id
            episode_mapping[tvmaze_id].append(episode)
        else:
            episode_mapping[id_mapping[episode['tvshowid']]].append(episode)
    for tvmaze_id, episodes in six.iteritems(episode_mapping):
        try:
            _send_show_episodes_to_tvmaze(tvmaze_id, episodes)
        except UpdateError as exc:
            errors = True
            logger.error(
                'Unable to update episodes for show {}: {}'.format(tvmaze_id, exc))
            continue
    if errors:
        DIALOG.notification(ADDON_ID, _('Update completed with errors'), icon='error')
    else:
        DIALOG.notification(ADDON_ID, _('Update completed'), icon=ICON, time=3000, sound=False)


def get_menu_actions():
    # type: () -> List[Tuple[Text, Callable[[], None]]]
    """
    Get main menu actions

    :return: the list of tuples (menu_label, action_callable)
    """
    actions = [(_('Authorize the addon'), authorize_addon)]
    if is_authorized():
        actions = [
            (_('Update all shows'), update_all_episodes),
            (_('Update recent episodes'), update_recent_episodes),
        ] + actions
    return actions
