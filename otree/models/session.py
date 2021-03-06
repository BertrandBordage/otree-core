#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging

import django.test

from otree import constants_internal
import otree.common_internal
from otree.common_internal import random_chars_8, random_chars_10
from otree.db import models
from .varsmixin import ModelWithVars
from otree.models_concrete import ParticipantToPlayerLookup
from otree.models_concrete import RoomSession


logger = logging.getLogger('otree')

client = django.test.Client()


class GlobalSingleton(models.Model):
    """object that can hold site-wide settings. There should only be one
    GlobalSingleton object. Also used for wait page actions.
    """

    class Meta:
        app_label = "otree"

    locked = models.BooleanField(default=False)


# for now removing SaveTheChange
class Session(ModelWithVars):

    class Meta:
        # if i don't set this, it could be in an unpredictable order
        ordering = ['pk']
        app_label = "otree"

    config = models.JSONField(
        default=dict, null=True,
        doc=("the session config dict, as defined in the "
             "programmer's settings.py."))

    # label of this session instance
    label = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    experimenter_name = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='For internal record-keeping')

    code = models.CharField(
        default=random_chars_8,
        max_length=16,
        null=False,
        unique=True,
        db_index=True,
        doc="Randomly generated unique identifier for the session.")

    time_scheduled = models.DateTimeField(
        null=True, doc="The time at which the session is scheduled",
        help_text='For internal record-keeping', blank=True)

    time_started = models.DateTimeField(
        null=True,
        doc="The time at which the experimenter started the session")

    mturk_HITId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')
    mturk_HITGroupId = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Hit id for this session on MTurk')
    mturk_qualification_type_id = models.CharField(
        max_length=300, null=True, blank=True,
        help_text='Qualification type that is '
                  'assigned to each worker taking hit')

    # since workers can drop out number of participants on server should be
    # greater than number of participants on mturk
    # value -1 indicates that this session it not intended to run on mturk
    mturk_num_participants = models.IntegerField(
        default=-1,
        help_text="Number of participants on MTurk")

    mturk_sandbox = models.BooleanField(
        default=True,
        help_text="Should this session be created in mturk sandbox?")

    archived = models.BooleanField(
        default=False,
        db_index=True,
        doc=("If set to True the session won't be visible on the "
             "main ViewList for sessions"))

    git_commit_timestamp = models.CharField(
        max_length=200, null=True,
        doc=(
            "Indicates the version of the code (as recorded by Git) that was "
            "used to run the session, so that the session can be replicated "
            "later.\n Search through the Git commit log to find a commit that "
            "was made at this time."))

    comment = models.TextField(blank=True)

    _anonymous_code = models.CharField(
        default=random_chars_10, max_length=8, null=False, db_index=True)

    special_category = models.CharField(
        db_index=True, max_length=20, null=True,
        doc="whether it's a test session, demo session, etc.")

    _pre_create_id = models.CharField(max_length=300, db_index=True, null=True)

    def __unicode__(self):
        return self.code

    @property
    def participation_fee(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['participation_fee']

    @property
    def real_world_currency_per_point(self):
        '''This method is deprecated from public API,
        but still useful internally (like data export)'''
        return self.config['real_world_currency_per_point']

    def is_for_mturk(self):
        return (not self.is_demo()) and (self.mturk_num_participants > 0)

    def is_demo(self):
        return (
            self.special_category ==
            constants_internal.session_special_category_demo
        )

    def get_subsessions(self):
        lst = []
        app_sequence = self.config['app_sequence']
        for app in app_sequence:
            models_module = otree.common_internal.get_models_module(app)
            subsessions = models_module.Subsession.objects.filter(
                session=self
            ).order_by('round_number')
            lst.extend(list(subsessions))
        return lst

    def delete(self, using=None):
        for subsession in self.get_subsessions():
            subsession.delete()
        super(Session, self).delete(using)

    def get_participants(self):
        return self.participant_set.all()

    def _create_groups_and_initialize(self):
        # group_by_arrival_time code used to be here
        for subsession in self.get_subsessions():
            subsession._create_groups()
            subsession._initialize()
            subsession.save()
        # assert self is subsession.session
        self.save()

    def mturk_requester_url(self):
        if self.mturk_sandbox:
            requester_url = (
                "https://requestersandbox.mturk.com/mturk/manageHITs"
            )
        else:
            requester_url = "https://requester.mturk.com/mturk/manageHITs"
        return requester_url

    def mturk_worker_url(self):
        if self.mturk_sandbox:
            worker_url = (
                "https://workersandbox.mturk.com/mturk/preview?groupId={}"
            ).format(self.mturk_HITGroupId)
        else:
            worker_url = (
                "https://www.mturk.com/mturk/preview?groupId={}"
            ).format(self.mturk_HITGroupId)
        return worker_url

    def advance_last_place_participants(self):
        participants = self.get_participants()

        # in case some participants haven't started
        unvisited_participants = []
        for p in participants:
            if not p._current_form_page_url:
                unvisited_participants.append(p)
                client.get(p._start_url(), follow=True)

        if unvisited_participants:
            from otree.models import Participant
            for p in unvisited_participants:
                p.save()
                Participant.flush_cached_instance(p)
            # that's it -- just visit the start URL, advancing by 1
            return

        last_place_page_index = min([p._index_in_pages for p in participants])
        last_place_participants = [
            p for p in participants
            if p._index_in_pages == last_place_page_index
        ]

        for p in last_place_participants:
            # what if first page is wait page?
            # that shouldn't happen, because then they must be
            # waiting for some other players who are even further back
            assert p._current_form_page_url
            try:
                resp = client.post(
                    p._current_form_page_url,
                    data={constants_internal.auto_submit: True}, follow=True
                )
            except:
                logging.exception("Failed to advance participants.")
                raise

            assert resp.status_code < 400

    def build_participant_to_player_lookups(self):
        subsession_app_names = self.config['app_sequence']

        views_modules = {}
        for app_name in subsession_app_names:
            views_modules[app_name] = (
                otree.common_internal.get_views_module(app_name))

        def views_module_for_player(player):
            return views_modules[player._meta.app_config.name]

        records_to_create = []

        for participant in self.get_participants():
            page_index = 0
            for player in participant.get_players():
                for View in views_module_for_player(player).page_sequence:
                    page_index += 1
                    records_to_create.append(ParticipantToPlayerLookup(
                        participant_pk=participant.pk,
                        page_index=page_index,
                        app_name=player._meta.app_config.name,
                        player_pk=player.pk,
                        url=View.url(participant, page_index)
                    ))

            # technically could be stored at the session level
            participant._max_page_index = page_index
            participant.save()
        ParticipantToPlayerLookup.objects.bulk_create(records_to_create)

    def get_room(self):
        from otree.room import ROOM_DICT
        room_name = RoomSession.objects.get(session_pk=self.pk).room_name
        if room_name:
            return ROOM_DICT[room_name]
        else:
            return None
