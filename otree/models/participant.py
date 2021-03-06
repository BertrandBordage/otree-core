#!/usr/bin/env python
# -*- coding: utf-8 -*-

import django.test

from otree import constants_internal
import otree.common_internal
from otree.common_internal import id_label_name, random_chars_8
from otree.common import Currency as c
from otree.db import models
from otree.models_concrete import ParticipantToPlayerLookup
from otree.models.session import Session
from otree.models.varsmixin import ModelWithVars


class Participant(ModelWithVars):

    class Meta:
        ordering = ['pk']
        app_label = "otree"
        index_together = ['session', 'mturk_worker_id', 'mturk_assignment_id']

    exclude_from_data_analysis = models.BooleanField(
        default=False, doc=(
            "if set to 1, the experimenter indicated that this participant's "
            "data points should be excluded from the data analysis (e.g. a "
            "problem took place during the experiment)"
        )
    )

    session = models.ForeignKey(Session)
    time_started = models.DateTimeField(null=True)
    user_type_in_url = constants_internal.user_type_participant
    mturk_assignment_id = models.CharField(
        max_length=50, null=True)
    mturk_worker_id = models.CharField(max_length=50, null=True)

    start_order = models.PositiveIntegerField(db_index=True)

    # unique=True can't be set, because the same external ID could be reused
    # in multiple sequences. however, it should be unique within the sequence.
    label = models.CharField(
        max_length=50, null=True, doc=(
            "Label assigned by the experimenter. Can be assigned by passing a "
            "GET param called 'participant_label' to the participant's start "
            "URL"
        )
    )

    _index_in_subsessions = models.PositiveIntegerField(default=0, null=True)

    _index_in_pages = models.PositiveIntegerField(default=0, db_index=True)

    id_in_session = models.PositiveIntegerField(null=True)

    def _id_in_session(self):
        """the human-readable version."""
        return 'P{}'.format(self.id_in_session)

    _waiting_for_ids = models.CharField(null=True, max_length=300)

    code = models.CharField(
        default=random_chars_8,
        max_length=16,
        null=False,
        db_index=True,
        unique=True,
        doc=(
            "Randomly generated unique identifier for the participant. If you "
            "would like to merge this dataset with those from another "
            "subsession in the same session, you should join on this field, "
            "which will be the same across subsessions."
        )
    )

    last_request_succeeded = models.BooleanField(
        verbose_name='Health of last server request'
    )

    visited = models.BooleanField(
        default=False, db_index=True,
        doc="""Whether this user's start URL was opened"""
    )

    ip_address = models.GenericIPAddressField(null=True)

    # stores when the page was first visited
    _last_page_timestamp = models.PositiveIntegerField(null=True)

    _last_request_timestamp = models.PositiveIntegerField(null=True)

    is_on_wait_page = models.BooleanField(default=False)

    # these are both for the admin
    # In the changelist, simply call these "page" and "app"
    _current_page_name = models.CharField(max_length=200, null=True,
                                          verbose_name='page')
    _current_app_name = models.CharField(max_length=200, null=True,
                                         verbose_name='app')

    # only to be displayed in the admin participants changelist
    _round_number = models.PositiveIntegerField(
        null=True
    )

    _current_form_page_url = models.URLField()

    _max_page_index = models.PositiveIntegerField()

    _is_auto_playing = models.BooleanField(default=False)

    def _start_auto_play(self):
        self._is_auto_playing = True
        self.save()

        client = django.test.Client()

        if not self.visited:
            client.get(self._start_url(), follow=True)

    def _stop_auto_play(self):
        self._is_auto_playing = False
        self.save()

    def player_lookup(self):
        # this is the most reliable way to get the app name,
        # because of WaitUntilAssigned...
        # 2016-04-07: WaitUntilAssigned removed
        return ParticipantToPlayerLookup.objects.get(
            participant_pk=self.pk,
            page_index=self._index_in_pages)

    def _current_page(self):
        return '{}/{} pages'.format(self._index_in_pages, self._max_page_index)

    def get_players(self):
        """Used to calculate payoffs"""
        lst = []
        app_sequence = self.session.config['app_sequence']
        for app in app_sequence:
            models_module = otree.common_internal.get_models_module(app)
            players = models_module.Player.objects.filter(
                participant=self
            ).order_by('round_number')
            lst.extend(list(players))
        return lst

    def status(self):
        # TODO: status could be a field that gets set imperatively
        if not self.visited:
            return 'Not visited yet'
        if self.is_on_wait_page:
            if self._waiting_for_ids:
                return 'Waiting for {}'.format(self._waiting_for_ids)
            return 'Waiting'
        return 'Playing'

    def _url_i_should_be_on(self):
        if self._index_in_pages <= self._max_page_index:
            return self.player_lookup().url
        else:
            if self.session.mturk_HITId:
                assignment_id = self.mturk_assignment_id
                if self.session.mturk_sandbox:
                    url = (
                        'https://workersandbox.mturk.com/mturk/externalSubmit'
                    )
                else:
                    url = "https://www.mturk.com/mturk/externalSubmit"
                url = otree.common_internal.add_params_to_url(
                    url,
                    {
                        'assignmentId': assignment_id,
                        'extra_param': '1'  # required extra param?
                    }
                )
                return url
            from otree.views.concrete import OutOfRangeNotification
            return OutOfRangeNotification.url(self)

    def __unicode__(self):
        return self.name()

    def _start_url(self):
        return '/InitializeParticipant/{}'.format(self.code)

    @property
    def payoff(self):
        return sum(player.payoff or c(0) for player in self.get_players())

    def payoff_in_real_world_currency(self):
        return self.payoff.to_real_world_currency(
            self.session
        )

    def payoff_from_subsessions(self):
        """Deprecated on 2015-05-07.
        Remove at some point.
        """
        return self.payoff

    def money_to_pay(self):
        return (
            self.session.config['participation_fee'] +
            self.payoff.to_real_world_currency(self.session)
        )

    def total_pay(self):
        return self.money_to_pay()

    def payoff_is_complete(self):
        return all(p.payoff is not None for p in self.get_players())

    def name(self):
        return id_label_name(self.pk, self.label)
