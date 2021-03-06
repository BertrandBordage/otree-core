#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import random
import six
from functools import reduce
from collections import OrderedDict
import sys


from django.conf import settings
from django.db import transaction
from django.db.utils import OperationalError

from six.moves import range
from six.moves import zip

import otree.db.idmap
from otree import constants_internal
from otree.models.session import Session
from otree.models.participant import Participant
from otree.common_internal import (
    get_models_module, get_app_constants,
    min_players_multiple)
from otree.common import RealWorldCurrency
from decimal import Decimal
from otree import deprecate
from otree.models_concrete import ParticipantLockModel


def gcd(a, b):
    """Return greatest common divisor using Euclid's Algorithm."""
    while b:
        a, b = b, a % b
    return a


def lcm(a, b):
    """Return lowest common multiple."""
    return a * b // gcd(a, b)


def lcmm(*args):
    """Return lcm of args."""
    return reduce(lcm, args)


def get_lcm(session_config):
    min_multiple_list = []
    for app_name in session_config['app_sequence']:
        app_constants = get_app_constants(app_name)
        # if players_per_group is None, 0, etc.
        min_multiple = min_players_multiple(
            app_constants.players_per_group)
        min_multiple_list.append(min_multiple)
    return lcmm(*min_multiple_list)


def validate_session_config(session_config):

    required_keys = {
        'name',
        'app_sequence',
        'participation_fee',
        'num_bots',
        'display_name',
        'real_world_currency_per_point',
        'num_demo_participants',
        'doc',
    }

    for key in required_keys:
        if key not in session_config:
            msg = ('Required key "{}" is missing from '
                   'session_config: {} dictionary')
            raise AttributeError(msg.format(key, session_config))

    st_name = session_config['name']
    if not re.match(r'^\w+$', st_name):
        msg = (
            'Session "{}": name must be alphanumeric with no '
            'spaces (underscores allowed).')
        raise ValueError(msg.format(st_name))

    app_sequence = session_config['app_sequence']
    if len(app_sequence) != len(set(app_sequence)):
        msg = (
            'app_sequence of "{}" in settings.py '
            'must not contain duplicate elements. '
            'If you want multiple rounds, '
            'you should set Constants.num_rounds.')
        raise ValueError(msg.format(session_config['name']))

    if len(app_sequence) == 0:
        raise ValueError('Need at least one subsession.')


def augment_session_config(session_config):
    new_session_config = {'doc': ''}
    new_session_config.update(settings.SESSION_CONFIG_DEFAULTS)
    new_session_config.update(session_config)

    # look up new_session_config
    # 2015-05-14: why do we strip? the doc can have line breaks in the middle
    # anyways
    new_session_config['doc'] = new_session_config['doc'].strip()

    # TODO: fixed_pay is deprecated as of 2015-05-07,
    # in favor of participation_fee. make this required at some point.
    if (('participation_fee' not in new_session_config) and
            ('fixed_pay' in new_session_config)):
        deprecate.dwarning(
            '"fixed_pay" is deprecated; '
            'you should rename it to "participation_fee".'
        )
        new_session_config['participation_fee'] = (
            new_session_config['fixed_pay'])

    new_session_config['participation_fee'] = RealWorldCurrency(
        new_session_config['participation_fee'])

    # normalize to decimal so we can do multiplications, etc
    # quantize because the original value may be a float,
    # which when converted to Decimal may have some 'decimal junk'
    # like 0.010000000000000000208166817...
    new_session_config['real_world_currency_per_point'] = Decimal(
        new_session_config['real_world_currency_per_point']
    ).quantize(Decimal('0.00001'))

    validate_session_config(new_session_config)
    return new_session_config

SESSION_CONFIGS_DICT = OrderedDict()
for config in settings.SESSION_CONFIGS:
    SESSION_CONFIGS_DICT[config['name']] = augment_session_config(config)


def app_labels_from_sessions(config_names):
    apps = set()
    for config_name in config_names:
        config = SESSION_CONFIGS_DICT[config_name]
        apps.update(config["app_sequence"])
    return apps


@transaction.atomic
def create_session(session_config_name, label='', num_participants=None,
                   special_category=None, _pre_create_id=None,
                   room=None, for_mturk=False):

    # 2014-5-2: i could implement this by overriding the __init__ on the
    # Session model, but I don't really know how that works, and it seems to
    # be a bit discouraged: http://goo.gl/dEXZpv
    # 2014-9-22: preassign to groups for demo mode.

    otree.db.idmap.activate_cache()

    try:
        session_config = SESSION_CONFIGS_DICT[session_config_name]
    except KeyError:
        msg = 'Session type "{}" not found in settings.py'
        raise ValueError(msg.format(session_config_name))
    session = Session.objects.create(
        config=session_config,
        label=label,
        special_category=special_category,
        _pre_create_id=_pre_create_id,)

    def bulk_create(model, descriptions):
        model.objects.bulk_create([
            model(session=session, **description)
            for description in descriptions])
        return model.objects.filter(session=session).order_by('pk')

    if num_participants is None:
        c_special_catdemo = constants_internal.session_special_category_demo
        c_special_catbots = constants_internal.session_special_category_bots
        if special_category == c_special_catdemo:
            num_participants = session_config['num_demo_participants']
        elif special_category == c_special_catbots:
            num_participants = session_config['num_bots']

    # check that it divides evenly
    session_lcm = get_lcm(session_config)
    if num_participants % session_lcm:
        msg = (
            'Session Config {}: Number of participants ({}) does not divide '
            'evenly into group size ({})')
        raise ValueError(
            msg.format(session_config['name'], num_participants, session_lcm))

    if for_mturk:
        session.mturk_num_participants = (
                num_participants /
                settings.MTURK_NUM_PARTICIPANTS_MULT)

    start_order = list(range(num_participants))
    if session_config.get('random_start_order'):
        random.shuffle(start_order)

    participants = bulk_create(
        Participant,
        [{'id_in_session': i + 1, 'start_order': j}
         for i, j in enumerate(start_order)])

    for participant in participants:
        ParticipantLockModel(participant_code=participant.code).save()

    try:
        for app_name in session_config['app_sequence']:

            models_module = get_models_module(app_name)
            app_constants = get_app_constants(app_name)

            round_numbers = list(range(1, app_constants.num_rounds + 1))

            subs = bulk_create(
                models_module.Subsession,
                [{'round_number': round_number} for round_number in round_numbers])

            # Create players
            models_module.Player.objects.bulk_create([
                models_module.Player(
                    session=session,
                    subsession=subsession,
                    round_number=round_number,
                    participant=participant)
                for round_number, subsession in zip(round_numbers, subs)
                for participant in participants])

        session._create_groups_and_initialize()
    # handle case where DB has missing column or table
    # missing table: OperationalError: no such table: pg_subsession
    # missing column: OperationalError: table pg_player has no column named contribution2
    except OperationalError as exception:
        exception_str = str(exception)
        if 'table' in exception_str:
            six.reraise(
                type(exception),
                type(exception)('{} - Try resetting the database.'.format(exception_str)),
                sys.exc_info()[2])
        raise


    session.build_participant_to_player_lookups()
    if room is not None:
        room.session = session
    session.save()
    otree.db.idmap.deactivate_cache()

    return session


default_app_config = 'otree.session.apps.OtreeSessionConfig'
