#!/usr/bin/env python
# -*- coding: utf-8 -*-

import collections
import sys
import uuid
import itertools
import os

from six.moves import range
from six.moves import zip

from django.http import HttpResponseRedirect, JsonResponse
from django.core.urlresolvers import reverse
from django.forms.forms import pretty_name
from django.conf import settings
from django.contrib import messages
from django.utils.encoding import force_text

import channels
import vanilla

from ordered_set import OrderedSet as oset
from collections import OrderedDict

import easymoney

import otree.constants_internal
import otree.models.session
from otree.common_internal import (
    get_models_module, app_name_format,
    channels_create_session_group_name,
    db_status_ok,
    check_pypi_for_updates)
from otree.session import SESSION_CONFIGS_DICT, get_lcm
from otree import forms
from otree.forms import widgets
from otree.common import RealWorldCurrency
from otree.views.abstract import GenericWaitPageMixin, AdminSessionPageMixin
from otree.views.mturk import MTurkConnection, get_workers_by_status
from otree.common import Currency as c
from otree.models.session import Session
from otree.models.participant import Participant
from otree.models_concrete import PageCompletion
from otree.room import ROOM_DICT


def get_all_fields(Model, for_export=False):
    if Model is PageCompletion:
        return [
            'session_pk',
            'participant_pk',
            'page_index',
            'app_name',
            'page_name',
            'time_stamp',
            'seconds_on_page',
            'subsession_pk',
            'auto_submitted',
        ]

    if Model is Session:
        return [
            'code',
            'label',
            'experimenter_name',
            'real_world_currency_per_point',
            'time_scheduled',
            'time_started',
            'mturk_HITId',
            'mturk_HITGroupId',
            'participation_fee',
            'comment',
            'special_category',
        ]

    if Model is Participant:
        if for_export:
            return [
                '_id_in_session',
                'code',
                'label',
                '_current_page',
                '_current_app_name',
                '_round_number',
                '_current_page_name',
                'status',
                'last_request_succeeded',
                'ip_address',
                'time_started',
                'exclude_from_data_analysis',
                'name',
                'session',
                'visited',
                'mturk_worker_id',
                'mturk_assignment_id',
            ]
        else:
            # not used; see ParticipantSerializer
            return []

    first_fields = {
        'Player':
            [
                'id_in_group',
                'role',
            ],
        'Group':
            [
                'id',
            ],
        'Subsession':
            [],
    }[Model.__name__]
    first_fields = oset(first_fields)

    last_fields = {
        'Player': [],
        'Group': [],
        'Subsession': [],
    }[Model.__name__]
    last_fields = oset(last_fields)

    fields_for_export_but_not_view = {
        'Player': {'id', 'label', 'subsession', 'session'},
        'Group': {'id'},
        'Subsession': {'id', 'round_number'},
    }[Model.__name__]

    fields_for_view_but_not_export = {
        'Player': set(),
        'Group': {'subsession', 'session'},
        'Subsession': {'session'},
    }[Model.__name__]

    fields_to_exclude_from_export_and_view = {
        'Player': {
            '_index_in_game_pages',
            'participant',
            'group',
            'subsession',
            'session',
            'round_number',
        },
        'Group': {
            'subsession',
            'id_in_subsession',
            'session',
            '_is_missing_players',
            'round_number',
        },
        'Subsession': {
            'code',
            'label',
            'session',
            'session_access_code',
        },
    }[Model.__name__]

    if for_export:
        fields_to_exclude = fields_to_exclude_from_export_and_view.union(
            fields_for_view_but_not_export
        )
    else:
        fields_to_exclude = fields_to_exclude_from_export_and_view.union(
            fields_for_export_but_not_view
        )

    all_fields_in_model = oset([field.name for field in Model._meta.fields])

    middle_fields = (
        all_fields_in_model - first_fields - last_fields - fields_to_exclude
    )

    return list(first_fields | middle_fields | last_fields)


def get_display_table_rows(app_name, for_export, subsession_pk=None):
    if not for_export and not subsession_pk:
        raise ValueError("if this is for the admin results table, "
                         "you need to specify a subsession pk")
    models_module = otree.common_internal.get_models_module(app_name)
    Player = models_module.Player
    Group = models_module.Group
    Subsession = models_module.Subsession
    if for_export:
        model_order = [
            Participant,
            Player,
            Group,
            Subsession,
            Session
        ]
    else:
        model_order = [
            Player,
            Group,
            Subsession,
        ]

    # get title row
    all_columns = []
    for Model in model_order:
        field_names = get_all_fields(Model, for_export)
        columns_for_this_model = [
            (Model, field_name) for field_name in field_names
            ]
        all_columns.extend(columns_for_this_model)

    if subsession_pk:
        # we had a strange result on one person's heroku instance
        # where Meta.ordering on the Player was being ingnored
        # when you use a filter. So we add one explicitly.
        players = Player.objects.filter(
            subsession_id=subsession_pk).order_by('pk')
    else:
        players = Player.objects.all()
    session_ids = set([player.session_id for player in players])

    # initialize
    parent_objects = {}

    parent_models = [
        Model for Model in model_order if Model not in {Player, Session}
        ]

    for Model in parent_models:
        parent_objects[Model] = {
            obj.pk: obj
            for obj in Model.objects.filter(session_id__in=session_ids)
            }

    if Session in model_order:
        parent_objects[Session] = {
            obj.pk: obj for obj in Session.objects.filter(pk__in=session_ids)
            }

    all_rows = []
    for player in players:
        row = []
        for column in all_columns:
            Model, field_name = column
            if Model == Player:
                model_instance = player
            else:
                fk_name = Model.__name__.lower()
                parent_object_id = getattr(player, "{}_id".format(fk_name))
                if parent_object_id is None:
                    model_instance = None
                else:
                    model_instance = parent_objects[Model][parent_object_id]

            attr = getattr(model_instance, field_name, '')
            if isinstance(attr, collections.Callable):
                if Model == Player and field_name == 'role' \
                        and model_instance.group is None:
                    attr = ''
                else:
                    try:
                        attr = attr()
                    except:
                        attr = "(error)"
            row.append(attr)
        all_rows.append(row)

    values_to_replace = {None: '', True: 1, False: 0}

    for row in all_rows:
        for i in range(len(row)):
            value = row[i]
            try:
                replace = value in values_to_replace
            except TypeError:
                # if it's an unhashable data type
                # like Json or Pickle field
                replace = False
            if replace:
                value = values_to_replace[value]
            elif for_export and isinstance(value, easymoney.Money):
                # remove currency formatting for easier analysis
                value = easymoney.to_dec(value)
            value = force_text(value)
            value = value.replace('\n', ' ').replace('\r', ' ')
            row[i] = value

    column_display_names = []
    for Model, field_name in all_columns:
        column_display_names.append(
            (Model.__name__, field_name)
        )

    return column_display_names, all_rows


class CreateSessionForm(forms.Form):
    session_configs = SESSION_CONFIGS_DICT.values()
    session_config_choices = (
        [('', '-----')] +
        [(s['name'], s['display_name']) for s in session_configs])

    session_config = forms.ChoiceField(
        choices=session_config_choices, required=True)

    num_participants = forms.IntegerField()

    def __init__(self, *args, **kwargs):
        for_mturk = kwargs.pop('for_mturk')
        super(CreateSessionForm, self).__init__(*args, **kwargs)
        if for_mturk:
            self.fields['num_participants'].label = "Number of workers"
            self.fields['num_participants'].help_text = (
                'Since workers can return the hit or drop out '
                '"spare" participants will be created. Namely server will '
                'have %s times more participants than MTurk HIT. '
                'The number you enter in this field is number of '
                'workers required for your HIT.'
                % settings.MTURK_NUM_PARTICIPANTS_MULT
            )
        else:
            self.fields['num_participants'].label = "Number of participants"

    def clean_num_participants(self):
        session_config_name = self.cleaned_data.get('session_config')

        # We must check for an empty string in case validation is not run
        if session_config_name != '':
            lcm = get_lcm(SESSION_CONFIGS_DICT[session_config_name])
            num_participants = self.cleaned_data['num_participants']
            if num_participants % lcm:
                raise forms.ValidationError(
                    'Please enter a valid number of participants.'
                )
            return num_participants


class CreateSession(vanilla.FormView):
    form_class = CreateSessionForm
    template_name = 'otree/admin/CreateSession.html'

    @classmethod
    def url_pattern(cls):
        return r"^create_session/$"

    @classmethod
    def url_name(cls):
        return 'session_create'

    def dispatch(self, request, *args, **kwargs):
        self.for_mturk = (int(self.request.GET.get('mturk', 0)) == 1)
        return super(CreateSession, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        session_config_summaries = [
            info_about_session_config(session_config)
            for session_config in SESSION_CONFIGS_DICT.values()]
        kwargs.update({'session_config_summaries': session_config_summaries})
        return super(CreateSession, self).get_context_data(**kwargs)

    def get_form(self, data=None, files=None, **kwargs):
        kwargs['for_mturk'] = self.for_mturk
        return super(CreateSession, self).get_form(data, files, **kwargs)

    def form_valid(self, form):
        pre_create_id = uuid.uuid4().hex
        kwargs = {
            'session_config_name': form.cleaned_data['session_config'],
            '_pre_create_id': pre_create_id,
            'for_mturk': self.for_mturk
        }
        if self.for_mturk:
            kwargs['num_participants'] = (
                form.cleaned_data['num_participants'] *
                settings.MTURK_NUM_PARTICIPANTS_MULT
            )

        else:
            kwargs['num_participants'] = form.cleaned_data['num_participants']

        # TODO:
        # Refactor when we upgrade to push
        if hasattr(self, "room"):
            kwargs['room'] = self.room

        channels_group_name = channels_create_session_group_name(
            pre_create_id)
        channels.Channel('otree.create_session').send({
            'kwargs': kwargs,
            'channels_group_name': channels_group_name
        })

        wait_for_session_url = reverse(
            'wait_for_session', args=(pre_create_id,)
        )
        return HttpResponseRedirect(wait_for_session_url)


class Rooms(vanilla.TemplateView):
    template_name = 'otree/admin/Rooms.html'

    @classmethod
    def url_pattern(cls):
        return r"^rooms/$"

    @classmethod
    def url_name(cls):
        return 'rooms'

    def get_context_data(self, **kwargs):
        return {'rooms': ROOM_DICT.values()}


class RoomWithoutSession(CreateSession):
    template_name = 'otree/admin/RoomWithoutSession.html'
    room = None

    @classmethod
    def url_pattern(cls):
        return r"^room_without_session/(?P<room_name>.+)/$"

    @classmethod
    def url_name(cls):
        return 'room_without_session'

    def dispatch(self, request, *args, **kwargs):
        self.room = ROOM_DICT[kwargs['room_name']]
        if self.room.has_session():
            return HttpResponseRedirect(
                reverse('room_with_session', args=[kwargs['room_name']]))
        return super(RoomWithoutSession, self).dispatch(
            request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        # TODO:
        # List names (or identifiers) of whos waiting
        # Display count of waiting participants
        context = {'participant_urls': self.room.get_participant_links(),
                   'participant_names': [],
                   'participant_count': str(0),
                   'room': self.room}
        kwargs.update(context)

        return super(RoomWithoutSession, self).get_context_data(**kwargs)

        # TODO:
        #
        # - override start links page (so need to store on the session that
        #   it's in this room? hm, no)
        #


class RoomWithSession(vanilla.TemplateView):
    template_name = 'otree/admin/RoomWithSession.html'
    room = None

    @classmethod
    def url_pattern(cls):
        return r"^room_with_session/(?P<room_name>.+)/$"

    @classmethod
    def url_name(cls):
        return 'room_with_session'

    def dispatch(self, request, *args, **kwargs):
        self.room = ROOM_DICT[kwargs['room_name']]
        if not self.room.has_session():
            return HttpResponseRedirect(
                reverse('room_without_session', args=[kwargs['room_name']]))
        return super(RoomWithSession, self).dispatch(
            request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = {'participant_urls': self.room.get_participant_links(),
                   'session_url': reverse('session_monitor',
                                          args=(self.room.session.pk,)),
                   'room': self.room}
        kwargs.update(context)

        return super(RoomWithSession, self).get_context_data(**kwargs)


class CloseRoom(vanilla.View):
    @classmethod
    def url_pattern(cls):
        return r"^CloseRoom/(?P<room_name>.+)/$"

    @classmethod
    def url_name(cls):
        return 'close_room'

    def dispatch(self, request, *args, **kwargs):
        self.room = ROOM_DICT[kwargs['room_name']]
        self.room.session = None
        return HttpResponseRedirect(
            reverse('room_without_session', args=[kwargs['room_name']]))


class WaitUntilSessionCreated(GenericWaitPageMixin, vanilla.GenericView):

    @classmethod
    def url_pattern(cls):
        return r"^WaitUntilSessionCreated/(?P<pre_create_id>.+)/$"

    @classmethod
    def url_name(cls):
        return 'wait_for_session'

    body_text = 'Waiting until session created'

    def _is_ready(self):
        try:
            self.session = Session.objects.get(
                _pre_create_id=self._pre_create_id
            )
            return True
        except Session.DoesNotExist:
            return False

    def _response_when_ready(self):
        session = self.session
        if session.is_for_mturk():
            session_home_url = reverse(
                'session_create_hit', args=(session.pk,)
            )
        # demo mode
        elif self.request.GET.get('fullscreen'):
            session_home_url = reverse(
                'session_fullscreen', args=(session.pk,))
        else:  # typical case
            session_home_url = reverse(
                'session_start_links', args=(session.pk,))

        return HttpResponseRedirect(session_home_url)

    def dispatch(self, request, *args, **kwargs):
        self._pre_create_id = kwargs['pre_create_id']
        return super(WaitUntilSessionCreated, self).dispatch(
            request, *args, **kwargs
        )

    def socket_url(self):
        return '/wait_for_session/{}/'.format(self._pre_create_id)


class SessionMonitor(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_monitor'

    def get_context_data(self, **kwargs):

        field_names = get_all_fields(Participant)
        rows = []
        for p in self.session.get_participants():
            row = []
            for fn in field_names:
                attr = getattr(p, fn)
                if isinstance(attr, collections.Callable):
                    attr = attr()
                row.append(attr)
            rows.append(row)

        context = super(SessionMonitor, self).get_context_data(**kwargs)
        context.update({
            'column_names': [
                pretty_name(field.strip('_')) for field in field_names
                ],
            'rows': rows,
        })
        return context


class EditSessionPropertiesForm(forms.ModelForm):
    participation_fee = forms.RealWorldCurrencyField(
        required=False,
        # it seems that if this is omitted, the step defaults to an integer,
        # meaninng fractional inputs are not accepted
        widget=widgets.RealWorldCurrencyInput(attrs={'step': 0.01})
    )
    real_world_currency_per_point = forms.DecimalField(
        decimal_places=5, max_digits=12,
        required=False
    )

    class Meta:
        model = Session
        fields = [
            'label',
            'experimenter_name',
            'time_scheduled',
            'comment',
        ]

    def __init__(self, *args, **kwargs):
        super(EditSessionPropertiesForm, self).__init__(*args, **kwargs)


class EditSessionProperties(AdminSessionPageMixin, vanilla.UpdateView):
    model = Session
    form_class = EditSessionPropertiesForm
    template_name = 'otree/admin/EditSessionProperties.html'

    def get_form(self, data=None, files=None, **kwargs):
        form = super(
            EditSessionProperties, self
        ).get_form(data, files, **kwargs)
        config = self.session.config
        form.fields[
            'participation_fee'
        ].initial = config['participation_fee']
        form.fields[
            'real_world_currency_per_point'
        ].initial = config['real_world_currency_per_point']
        if self.session.mturk_HITId:
            form.fields['participation_fee'].widget.attrs['readonly'] = 'True'
        return form

    @classmethod
    def url_name(cls):
        return 'session_edit'

    def get_success_url(self):
        return reverse('session_edit', args=(self.session.pk,))

    def form_valid(self, form):
        super(EditSessionProperties, self).form_valid(form)
        config = self.session.config
        participation_fee = form.cleaned_data[
            'participation_fee'
        ]
        real_world_currency_per_point = form.cleaned_data[
            'real_world_currency_per_point'
        ]
        if form.cleaned_data['participation_fee']:
            config['participation_fee'] = RealWorldCurrency(participation_fee)
        if form.cleaned_data['real_world_currency_per_point']:
            config[
                'real_world_currency_per_point'
            ] = real_world_currency_per_point
        # use .copy() to force marking this field as dirty/changed
        # FIXME: i don't need the below line anymore
        self.session.config = config.copy()
        self.session.save()
        messages.success(self.request, 'Properties have been updated')
        return HttpResponseRedirect(self.get_success_url())


class SessionPayments(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_payments'

    def get(self, *args, **kwargs):
        response = super(SessionPayments, self).get(*args, **kwargs)
        return response

    def get_context_data(self, **kwargs):
        session = self.session
        participants = session.get_participants()
        total_payments = 0.0
        mean_payment = 0.0
        if participants:
            total_payments = sum(
                part.money_to_pay() or c(0) for part in participants
            )
            mean_payment = total_payments / len(participants)

        context = super(SessionPayments, self).get_context_data(**kwargs)
        context.update({
            'participants': participants,
            'total_payments': total_payments,
            'mean_payment': mean_payment,
            'participation_fee': session.config['participation_fee'],
        })

        return context


class SessionMTurkPayments(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_mturk_payments'

    def get(self, *args, **kwargs):
        response = super(SessionMTurkPayments, self).get(*args, **kwargs)
        return response

    def get_context_data(self, **kwargs):
        session = self.session
        with MTurkConnection(
                self.request, session.mturk_sandbox
        ) as mturk_connection:
            workers_by_status = get_workers_by_status(
                mturk_connection,
                session.mturk_HITId
            )
            participants_not_reviewed = session.participant_set.filter(
                mturk_worker_id__in=workers_by_status['Submitted']
            )
            participants_approved = session.participant_set.filter(
                mturk_worker_id__in=workers_by_status['Approved']
            )
            participants_rejected = session.participant_set.filter(
                mturk_worker_id__in=workers_by_status['Rejected']
            )
        context = super(SessionMTurkPayments, self).get_context_data(**kwargs)
        context.update({
            'participants_approved': participants_approved,
            'participants_rejected': participants_rejected,
            'participants_not_reviewed': participants_not_reviewed,
            'participation_fee': session.config['participation_fee'],
        })

        return context


class SessionStartLinks(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_start_links'

    def get_context_data(self, **kwargs):
        session = self.session

        participant_urls = [
            self.request.build_absolute_uri(participant._start_url())
            for participant in session.get_participants()
            ]

        anonymous_url = self.request.build_absolute_uri(
            reverse(
                'join_session_anonymously',
                args=(session._anonymous_code,)
            )
        )

        context = super(SessionStartLinks, self).get_context_data(**kwargs)

        context.update({
            'participant_urls': participant_urls,
            'anonymous_url': anonymous_url,
            'num_participants': len(participant_urls),
            'fullscreen_mode_on': len(participant_urls) <= 3
        })
        return context


class SessionStartLinksRoom(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_start_links_room'

    def get_context_data(self, **kwargs):
        session = self.session
        room = session.get_room()

        context = {'participant_urls': room.get_participant_links(),
                   'room': room}
        kwargs.update(context)

        return super(SessionStartLinksRoom, self).get_context_data(**kwargs)


class SessionResults(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_results'

    def get_context_data(self, **kwargs):
        session = self.session

        participants = session.get_participants()
        participant_labels = [p._id_in_session() for p in participants]
        column_name_tuples = []
        rows = []

        for subsession in session.get_subsessions():
            app_label = subsession._meta.app_config.name

            column_names, subsession_rows = get_display_table_rows(
                subsession._meta.app_config.name,
                for_export=False,
                subsession_pk=subsession.pk
            )

            if not rows:
                rows = subsession_rows
            else:
                for i in range(len(rows)):
                    rows[i].extend(subsession_rows[i])

            round_number = subsession.round_number
            if round_number > 1:
                subsession_column_name = '{} [Round {}]'.format(
                    app_label, round_number
                )
            else:
                subsession_column_name = app_label

            for model_column_name, field_column_name in column_names:
                column_name_tuples.append(
                    (subsession_column_name,
                     model_column_name,
                     field_column_name)
                )

        subsession_headers = [
            (pretty_name(key), len(list(group)))
            for key, group in
            itertools.groupby(column_name_tuples, key=lambda x: x[0])
            ]

        model_headers = [
            (pretty_name(key[1]), len(list(group)))
            for key, group in
            itertools.groupby(column_name_tuples, key=lambda x: (x[0], x[1]))
            ]

        field_headers = [
            pretty_name(key[2]) for key, group in
            itertools.groupby(column_name_tuples, key=lambda x: x)
            ]

        # dictionary for json response
        # will be used only if json request  is done
        self.context_json = []
        for i, row in enumerate(rows):
            d_row = OrderedDict()
            d_row['participant_label'] = participant_labels[i]
            for t, v in zip(column_name_tuples, row):
                d_row['.'.join(t)] = v
            self.context_json.append(d_row)

        context = super(SessionResults, self).get_context_data(**kwargs)
        context.update({
            'subsession_headers': subsession_headers,
            'model_headers': model_headers,
            'field_headers': field_headers,
            'rows': rows})
        return context

    def get(self, request, *args, **kwargs):
        context = self.get_context_data()
        if self.request.META.get('CONTENT_TYPE') == 'application/json':
            return JsonResponse(self.context_json, safe=False)
        else:
            return self.render_to_response(context)


class SessionDescription(AdminSessionPageMixin, vanilla.TemplateView):
    @classmethod
    def url_name(cls):
        return 'session_description'

    def get_context_data(self, **kwargs):
        context = super(SessionDescription, self).get_context_data(**kwargs)
        context.update(session_description_dict(self.session))
        return context


def info_about_session_config(session_config):
    app_sequence = []
    for app_name in session_config['app_sequence']:
        models_module = get_models_module(app_name)
        num_rounds = models_module.Constants.num_rounds
        formatted_app_name = app_name_format(app_name)
        if num_rounds > 1:
            formatted_app_name = '{} ({} rounds)'.format(
                formatted_app_name, num_rounds
            )
        subsssn = {
            'doc': getattr(models_module, 'doc', ''),
            'bibliography': getattr(models_module, 'bibliography', []),
            'name': formatted_app_name,
        }
        app_sequence.append(subsssn)
    return {
        'doc': session_config['doc'],
        'app_sequence': app_sequence,
        'name': session_config['name'],
        'display_name': session_config['display_name'],
        'lcm': get_lcm(session_config)
    }


def session_description_dict(session):
    context_data = {
        'display_name': session.config['display_name'],
    }

    context_data.update(info_about_session_config(session.config))

    return context_data


class AdminHome(vanilla.ListView):
    template_name = 'otree/admin/Home.html'

    @classmethod
    def url_pattern(cls):
        return r"^sessions/(?P<archive>archive)?$"

    @classmethod
    def url_name(cls):
        return 'sessions'

    def get_context_data(self, **kwargs):
        context = super(AdminHome, self).get_context_data(**kwargs)
        context.update({
            'is_debug': settings.DEBUG,
        })
        return context

    def get_queryset(self):
        category = otree.constants_internal.session_special_category_demo
        return Session.objects.exclude(
            special_category=category).order_by('archived', '-pk')


class ServerCheck(vanilla.TemplateView):
    template_name = 'otree/admin/ServerCheck.html'

    @classmethod
    def url_pattern(cls):
        return r"^server_check/$"

    @classmethod
    def url_name(cls):
        return 'server_check'

    def app_is_on_heroku(self):
        return 'heroku' in self.request.get_host()

    def get_context_data(self, **kwargs):
        sqlite = settings.DATABASES['default']['ENGINE'].endswith('sqlite3')
        debug = settings.DEBUG
        update_message = check_pypi_for_updates(print_message=False)
        otree_version = otree.__version__
        regular_sentry = hasattr(settings, 'RAVEN_CONFIG')
        heroku_sentry = os.environ.get('SENTRY_DSN')
        sentry = regular_sentry or heroku_sentry
        auth_level = settings.AUTH_LEVEL in {'DEMO', 'STUDY'}
        heroku = self.app_is_on_heroku()
        runserver = 'runserver' in sys.argv
        db_synced = db_status_ok(cached_per_process=False)

        return {
            'sqlite': sqlite,
            'debug': debug,
            'update_message': update_message,
            'otree_version': otree_version,
            'sentry': sentry,
            'auth_level': auth_level,
            'heroku': heroku,
            'runserver': runserver,
            'db_synced': db_synced
        }