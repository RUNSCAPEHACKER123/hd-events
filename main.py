import cgi
from google.appengine.ext import db
from google.appengine.ext.webapp import util, template
from google.appengine.api import urlfetch, memcache, users, mail

import json
import unicodedata
from icalendar import Calendar, Event as CalendarEvent
import logging, urllib, os
from pprint import pprint
import cPickle as pickle
from datetime import datetime, timedelta

from models import Event, Feedback, HDLog, ROOM_OPTIONS, PENDING_LIFETIME
from utils import username, human_username, set_cookie, local_today, is_phone_valid, UserRights, dojo
from notices import *

import PyRSS2Gen
import pytz

import webapp2

from config import Config
import re
import keymaster

template.register_template_library("templatefilters.templatefilters")

def slugify(str):
    str = unicodedata.normalize('NFKD', str.lower()).encode('ascii','ignore')
    return re.sub(r'\W+','-',str)

def event_path(event):
    return '/event/%s-%s' % (event.key().id(), slugify(event.name))


""" Checks if a user needs to enter contact info for another member.
handler: The handler to read request parameters from.
start_time: Start time of the event. (datetime)
end_time: End time of the event. (datetime)
Returns: Either the member email that they entered, or an empty string if they
don't need to enter one. """
def _get_other_member(handler, start_time, end_time):
  if end_time - start_time < timedelta(hours=24):
    # No need to do this.
    return ""

  # If the event lasts for 24 hours or more, they must specify a
  # second person to be in charge of it.
  member = handler.request.get('other_member')
  if not member:
    raise ValueError('Need to specify second responsible member' \
                     ' for multi-day event.')

  conf = Config()
  if not conf.is_testing:
    # Make sure this person is a member.
    base_url = conf.SIGNUP_URL + '/api/v1/user'
    query = urllib.urlencode({'email': member, 'properties[]': ''})
    result = urlfetch.fetch("%s?%s" % (base_url, query),
                            follow_redirects=False)
    logging.debug("Got response: %s" % (result.content))

    if result.status_code != 200:
      # The API call failed.
      if result.status_code == 422:
        raise ValueError('\'%s\' is not the email of a member.' % \
                          (member))
      raise ValueError('Backend API call failed. Please try again' \
                      ' later.')

  return member


""" Checks that this particular user is clear to create an event. Mainly, this
means that they don't have too many future events already scheduled.
user: The user we are checking for. (GAE User() object.)
event_times: A list of tuples, with each tuple containing the start and end
times for a particular event. These are the events that we are checking if we
can create.
ignore_admin: Forces it to always perform the check as if the user were a
regular user. Defaults to False.
editing: The event that we are editing, if we are editing one. Defaults to
None. """
def _check_user_can_create(user, event_times, ignore_admin=False,
                           editing=None):
  logging.debug("User wants to add %d events." % (len(event_times)))

  # If they are an admin, they can do whatever they want.
  user_status = UserRights(user)
  if (not ignore_admin and user_status.is_admin):
    logging.info("User %s is admin, not performing checks." % (user.email()))
    return

  events_query = db.GqlQuery("SELECT * FROM Event WHERE member = :1 AND" \
                             " start_time > :2 AND status IN :3", user,
                             datetime.now(), ["approved", "pending", "on_hold"])

  num_events = events_query.count()
  logging.debug("User has %d events." % (num_events))
  num_events += len(event_times)
  # If we're editing events, subtract one so that we don't count the same event
  # twice.
  if editing:
    num_events -= 1

  conf = Config()
  if num_events > conf.USER_MAX_FUTURE_EVENTS:
    raise ValueError("You may only have %d future events." % \
                     (conf.USER_MAX_FUTURE_EVENTS))

  # We have a limit on how many events we can have within a four-week period
  # too.
  for start_time, end_time in event_times:
    # Find the subset of events that this event could possibly cause to be in
    # violation of this rule.
    earliest_start = start_time - timedelta(days=28)
    latest_start = start_time + timedelta(days=28)
    possible_violators = db.GqlQuery("SELECT * FROM Event WHERE member = :1 AND" \
                                     " start_time >= :2 AND" \
                                     " start_time <= :3 AND status IN :4" \
                                     " ORDER BY start_time",
                                     user, earliest_start, latest_start,
                                     ["approved", "pending", "on_hold"])

    # Find the subset of events we want to add that could be in violation of
    # this rule.
    possible_pending_violators = []
    for event in event_times:
      if (event[0] >= earliest_start and event[0] <= latest_start):
        possible_pending_violators.append(event[0])

    logging.debug("Have %d possible violators." % (possible_violators.count()))
    logging.debug("Have %d possible pending violators." % \
                  (len(possible_pending_violators)))

    if (possible_violators.count() + len(possible_pending_violators)) <= \
        conf.USER_MAX_FOUR_WEEKS:
      # There's no way we could be violating this rule.
      return

    # Group the possible violators into those before and after the event.
    before_event = []
    after_event = []
    for event in possible_violators:
      # If we are editing an event, ignore it, so that it doesn't get
      # double-counted.
      if (editing and event.key().id() == editing.key().id()):
        continue

      # Split it into groups of events that happen before and after our proposed
      # event.
      if event.start_time < start_time:
        before_event.append(event.start_time)
      else:
        after_event.append(event.start_time)

    # Do the same with the pending events we want to add.
    for pending_start in possible_pending_violators:
      if pending_start < start_time:
        before_event.append(pending_start)
      else:
        after_event.append(pending_start)

    # If we have extraneous events, it means that the rule was already violated,
    # or that it will be violated just by adding recurring events.
    if (len(before_event) > conf.USER_MAX_FOUR_WEEKS or \
        len(after_event) > conf.USER_MAX_FOUR_WEEKS):
      raise ValueError("You may only have %d events within a 4-week period." % \
                      (conf.USER_MAX_FOUR_WEEKS))

    # Recombine the lists.
    possible_violators = before_event
    # Sandwhich in the time of the event we want to create.
    possible_violators.append(start_time)
    possible_violators.extend(after_event)

    # Now look through every possible combination of USER_MAX_FOUR_WEEKS + 1
    # consecutive events and see if it violates our rule. (The + 1 is so that
    # every group we come up with will contain the event we are trying to add.)
    event_group = []
    for event_time in possible_violators:
      if len(event_group) < (conf.USER_MAX_FOUR_WEEKS + 1):
        # We don't have enough events yet to do anything.
        event_group.append(event_time)
        continue

      # On to the next group...
      event_group.pop(0)
      event_group.append(event_time)

      # Check that our current event group is valid.
      if event_group[len(event_group) - 1] - event_group[0] <= \
          timedelta(days=28):
        # Now if we were to create that event, we would have one too many events
        # in a four week period, meaning that this event violates the rule.
        raise ValueError("You may only have %d events within a 4-week period." % \
                        (conf.USER_MAX_FOUR_WEEKS))


""" Makes sure that a proposed event is valid.
handler: The handler handling the users request to create/change an event.
editing_event_id: The id of the event we are editing. We use this so that we can
ignore it when detecting conflicts.
ignore_admin: If true, it will ignore the user's possible admin status and
perform all checks as if they were a normal user. Defaults to False.
recurring: Whether this is a recurring event or not. Defaults to False.
Raises a ValueError if it detects a problem.
Returns: A list of tuples, one for each individual event. Each tuple
contains the event start time and event end time. """
def _validate_event(handler, editing_event_id=0, ignore_admin=False,
                    recurring=False):
  """ Find the next weekday after a given date.
  date: The date to look for a weekday after.
  weekday: The day of the week to look for. (The name of the day, e.g.
  'monday'.) """
  def find_next_weekday(date, weekday):
    # Convert the string weekday to a number.
    day_conversion = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                      "friday": 4, "saturday": 5, "sunday": 6}
    weekday = weekday.lower()
    weekday = day_conversion[weekday]

    # Calculate the next weekday.
    days_ahead = weekday - date.weekday()
    if days_ahead <= 0:
      # Target day already happened this week.
      days_ahead += 7
    return date + timedelta(days_ahead)

  user = users.get_current_user()
  start_time = datetime.strptime('%s %s:%s %s' % (
      handler.request.get('start_date'),
      handler.request.get('start_time_hour'),
      handler.request.get('start_time_minute'),
      handler.request.get('start_time_ampm')), '%m/%d/%Y %I:%M %p')
  end_time = datetime.strptime('%s %s:%s %s' % (
      handler.request.get('end_date'),
      handler.request.get('end_time_hour'),
      handler.request.get('end_time_minute'),
      handler.request.get('end_time_ampm')), '%m/%d/%Y %I:%M %p')
  conflicts = Event.check_conflict(
      start_time,end_time,
      handler.request.get('setup'),
      handler.request.get('teardown'),
      handler.request.get_all('rooms'),
      optional_existing_event_id=editing_event_id
  )

  # Get the number of times it repeats.
  if recurring:
    recurrence_data = handler.request.get("recurring-data")
    recurrence_data = json.loads(recurrence_data)

    repetitions = recurrence_data["repetitions"]
  else:
    repetitions = 1

  event_times = [(start_time, end_time)]
  event_length = end_time - start_time
  logging.debug("Length of event: %s" % (event_length))

  editing_event = None
  if editing_event_id:
    editing_event = Event.get_by_id(editing_event_id)

  for i in range(0, repetitions):
    logging.debug("Next event starting at: %s" % (start_time))

    _check_one_event_per_day(user, start_time, editing=editing_event,
                             ignore_admin=ignore_admin)

    if conflicts:
      if ("Deck" in handler.request.get_all('rooms') or \
          "Savanna" in handler.request.get_all('rooms')):
        raise ValueError('Room conflict detected <small>(Note: Deck &amp;' \
                          ' Savanna share the same area, two events cannot take' \
                          ' place at the same time in these rooms.)</small>')
      else:
        raise ValueError('Room conflict detected')
    if not handler.request.get('details'):
      raise ValueError('You must provide a description of the event')
    if not handler.request.get('estimated_size').isdigit():
      raise ValueError('Estimated number of people must be a number')
    if not int(handler.request.get('estimated_size')) > 0:
      raise ValueError('Estimated number of people must be greater then zero')
    if (end_time-start_time).days < 0:
      raise ValueError('End time must be after start time')
    if (handler.request.get('contact_phone') and not is_phone_valid(handler.request.get('contact_phone'))):
      raise ValueError('Phone number does not appear to be valid')
    if not handler.request.get_all('rooms'):
      raise ValueError('You must select a room to reserve.')

    # Figure out the start and end time of the next event.
    if recurring:
      # After we validate the last event, we're done.
      if i == (repetitions - 1):
        break

      frequency = recurrence_data["frequency"]
      if frequency == "monthly":
        day_number = recurrence_data["dayNumber"]
        day_name = recurrence_data["monthDay"]

        # Now we can figure out when the next one is. Start by extracting only
        # the number.
        day_number = int(day_number[0])
        # Get a date for the start of the correct week.
        # Annoyingly, timedelta doesn't support months so we have to add a month
        # the hard way.
        months = start_time.month + 1
        years = start_time.year
        if months > 12:
          months = months % 12
          years += 1

        next_month = start_time.replace(year=years, month=months,
                                        day=(day_number - 1) * 7)
        # Find the specified weekday.
        start_time = find_next_weekday(next_month, day_name)

      elif frequency == "weekly":
        # Just add a week to our current time.
        start_time += timedelta(days=7)

      elif frequency == "daily":
        # Add a day.
        start_time += timedelta(days=1)

        if recurrence_data["weekdaysOnly"]:
          # Add days until we get past the weekend.
          while start_time.weekday() >= 5:
            start_time += timedelta(days=1)

      else:
        # This is almost certainly a programming error.
        error = "Got unknown frequency for recurring event."
        logging.critical(error)
        raise RuntimeError(error)

      end_time = start_time + event_length
      event_times.append((start_time, end_time))


  _check_user_can_create(user, event_times, ignore_admin=ignore_admin,
                         editing=editing_event)

  return event_times


""" Makes sure that adding this event won't violate a rule against having more
than one event per day during Dojo hours. There are, of course, exceptions to
this rule for anyone on the @events team.
user: The user that is creating this event.
start_time: The proposed start time of the event.
ignore_admin: Forces it to always perform the check as if the user were a normal
user. Defaults to False.
editing: The event we are editing, if we are editing. """
def _check_one_event_per_day(user, start_time, editing=None,
                             ignore_admin=False):
  # If we're an admin, we can do anything we want.
  user_status = UserRights(user)
  if (not ignore_admin and user_status.is_admin):
    logging.info("User %s is admin, not performing check." % (user.email()))
    return

  # If it is on a weekend, we shouldn't check either.
  if start_time.weekday() > 4:
    logging.info("Not performing check because event is on the weekend.")
    return

  conf = Config()
  # The earliest and latest that other events during Dojo hours this day might
  # start.
  earliest_start = start_time.replace(hour=conf.EVENT_HOURS[0], minute=0,
                                      second=0, microsecond=0)
  latest_start = start_time.replace(hour=conf.EVENT_HOURS[1], minute=0, second=0,
                                    microsecond=0)
  logging.debug("earliest start: %s, latest start: %s" % \
                (earliest_start, latest_start))

  # Check that we are trying to make this event during coworking hours.
  if (start_time < earliest_start or start_time > latest_start):
    logging.debug("Event is not during coworking hours.")
    return

  event_query = db.GqlQuery("SELECT * FROM Event WHERE start_time >= :1 AND" \
                            " start_time < :2 AND status IN :3",
                            earliest_start, latest_start,
                            ["pending", "approved"])
  found_events = event_query.count()
  logging.debug("Found %d events." % (found_events))

  if editing:
    if (editing.start_time >= earliest_start and \
        editing.start_time <= latest_start):
      # In this case, our old event is going to show up in the query and cause
      # it to register one too many events.
      logging.debug("Removing old event from event count.")
      found_events -= 1

  if found_events >= 1:
    # We can't have another event that starts today.
    raise ValueError("Hacker Dojo does not have enough space for all of our" \
                     " events+meetings+startups. As a result, we have to" \
                     " limit events during coworking hours (Monday through" \
                     " Friday, 9AM-5PM). There is already an event booked" \
                     " for this date. Please try another date. Sorry about" \
                     " any inconvenience.")


""" Figure out how many days a user must wait before they can create an event.
user: The user we are getting information for.
Returns: How many more days the user must wait to create an event. """
def _get_user_wait_time(user):
  conf = Config()
  if not conf.is_prod:
    # Don't do this check if we're not on the production server.
    return 0

  if not user:
    # We'll perform the check when they are logged in.
    return 0

  # Make an API request to the signup app to get this information about the
  # user.
  base_url = conf.SIGNUP_URL + "/api/v1/user"
  query_str = urllib.urlencode({"email": user.email(), "properties": "created"})
  response = urlfetch.fetch("%s?%s" % (base_url, query_str),
                            follow_redirects=False)
  logging.debug("Got response from signup app: %s" % (response.content))

  if response.status_code != 200:
    logging.error("Failed to fetch user data, status %d." % \
                  (response.status_code))
    # Disable it to be safe.
    return conf.NEW_EVENT_WAIT_PERIOD

  result = json.loads(response.content)
  created = pickle.loads(str(result["created"]))
  logging.debug("User created at %s." % (created))

  # Check to see how long we have left.
  since_creation = datetime.now() - created
  to_wait = max(0, conf.NEW_EVENT_WAIT_PERIOD - since_creation.days)
  logging.debug("Days to wait: %d" % (to_wait))

  return to_wait


""" Performs an action on a single event.
event: The event object that we are working with.
action: A string specifying the action to perform.
user: The user who is performing this action. (User() object.)
check: If True, the it will check whether the action can be run, but won't
actually run it.
Returns: True if the action is performed or can be performed, False otherwise.
"""
def _do_event_action(event, action, user, check=False):
  access_rights = UserRights(user, event)

  desc = ''
  todo = None
  args = []
  if action.lower() == 'approve':
    if not access_rights.can_approve:
      return False
    if not check:
      notify_owner_approved(event)
    todo = event.approve
    desc = 'Approved event'

  elif action.lower() == 'notapproved':
    if not access_rights.can_not_approve:
      return False
    todo = event.not_approved
    desc = 'Event marked not approved'

  elif action.lower() == 'rsvp':
    if not user:
      return False
    todo = event.rsvp
    if not check:
      notify_owner_rsvp(event,user)

  elif action.lower() == 'staff':
    if not access_rights.can_staff:
      return False
    todo = event.add_staff
    args.append(user)
    desc = 'added self as staff'

  elif action.lower() == 'unstaff':
    if not access_rights.can_unstaff:
      return False
    todo = event.remove_staff
    args.append(user)
    desc = 'Removed self as staff'

  elif action.lower() == 'onhold':
    if not access_rights.can_cancel:
      return False
    todo = event.on_hold
    desc = 'Put event on hold'

  elif action.lower() == 'cancel':
    if not access_rights.can_cancel:
      return False
    todo = event.cancel
    desc = 'Cancelled event'

  elif action.lower() == 'delete':
    if not access_rights.can_delete:
      return False
    todo = event.delete
    desc = 'Deleted event'
    if not check:
      notify_deletion(event,user)

  elif action.lower() == 'undelete':
    if not access_rights.can_undelete:
      return False
    todo = event.undelete
    desc = 'Undeleted event'

  elif action.lower() == 'expire' and access_rights.is_admin:
    todo = event.expire
    desc = 'Expired event'

  else:
    logging.warning("Action '%s' was not recognized." % (action))

  if check:
    return True

  if desc != '':
    log = HDLog(event=event,description=desc)
    log.put()

  if todo:
    todo(*args)

  return True


class DomainCacheCron(webapp2.RequestHandler):
    def get(self):
        noop = dojo('/groups/events',force=True)


class ReminderCron(webapp2.RequestHandler):
    def get(self):
        self.response.out.write("REMINDERS")
        today = local_today()
        # remind everyone 3 days in advance they need to show up
        events = Event.all() \
            .filter('status IN', ['approved']) \
            .filter('reminded =', False) \
            .filter('start_time <', today + timedelta(days=3))
        for event in events:
            self.response.out.write(event.name)
            # only mail them if they created the event 2+ days ago
            if event.created < today - timedelta(days=2):
              schedule_reminder_email(event)
            event.reminded = True
            event.put()


class ExpireCron(webapp2.RequestHandler):
    def post(self):
        # Expire events marked to expire today
        today = local_today()
        events = Event.all() \
            .filter('status IN', ['pending', 'understaffed']) \
            .filter('expired >=', today) \
            .filter('expired <', today + timedelta(days=1))
        for event in events:
            event.expire()
            notify_owner_expired(event)


class ExpireReminderCron(webapp2.RequestHandler):
    def post(self):
        # Find events expiring in 10 days to warn owner
        ten_days = local_today() + timedelta(days=10)
        events = Event.all() \
            .filter('status IN', ['pending', 'understaffed']) \
            .filter('expired >=', ten_days) \
            .filter('expired <', ten_days + timedelta(days=1))
        for event in events:
            notify_owner_expiring(event)

class ExportHandler(webapp2.RequestHandler):
    def get(self, format):
        content_type, body = getattr(self, 'export_%s' % format)()
        self.response.headers['content-type'] = content_type
        self.response.out.write(body)

    def export_json(self):
        events = Event.get_recent_past_and_future()
        for k in self.request.GET:
            if self.request.GET[k] and k in ['member']:
                value = users.User(urllib.unquote(self.request.GET[k]))
            else:
                value = urllib.unquote(self.request.GET[k])
            events = events.filter('%s =' % k, value)
        events = map(lambda x: x.to_dict(summarize=True), events)
        return 'application/json', json.dumps(events)

    def export_ics(self):
        events = Event.get_recent_past_and_future()
        url_base = 'http://' + self.request.headers.get('host', 'events.hackerdojo.com')
        cal = Calendar()
        for event in events:
            iev = CalendarEvent()
            iev.add('summary', event.name if event.status == 'approved' else event.name + ' (%s)' % event.status.upper())
            # make verbose description with empty fields where information is missing
            ev_desc = '__Status: %s\n__Member: %s\n__Type: %s\n__Estimated size: %s\n__Info URL: %s\n__Fee: %s\n__Contact: %s, %s\n__Rooms: %s\n\n__Details: %s\n\n__Notes: %s' % (
                event.status,
                event.owner(),
                event.type,
                event.estimated_size,
                event.url,
                event.fee,
                event.contact_name,
                event.contact_phone,
                event.roomlist(),
                event.details,
                event.notes)
            # then delete the empty fields with a regex
            ev_desc = re.sub(re.compile(r'^__.*?:[ ,]*$\n*',re.M),'',ev_desc)
            ev_desc = re.sub(re.compile(r'^__',re.M),'',ev_desc)
            ev_url = url_base + event_path(event)
            iev.add('description', ev_desc + '\n--\n' + ev_url)
            iev.add('url', ev_url)
            if event.start_time:
              iev.add('dtstart', event.start_time.replace(tzinfo=pytz.timezone('US/Pacific')))
            if event.end_time:
              iev.add('dtend', event.end_time.replace(tzinfo=pytz.timezone('US/Pacific')))
            cal.add_component(iev)
        return 'text/calendar', cal.as_string()

    def export_large_ics(self):
        events = Event.get_recent_past_and_future()
        url_base = 'http://' + self.request.headers.get('host', 'events.hackerdojo.com')
        cal = Calendar()
        for event in events:
            iev = CalendarEvent()
            iev.add('summary', event.name + ' (%s)' % event.estimated_size)
            # make verbose description with empty fields where information is missing
            ev_desc = '__Status: %s\n__Member: %s\n__Type: %s\n__Estimated size: %s\n__Info URL: %s\n__Fee: %s\n__Contact: %s, %s\n__Rooms: %s\n\n__Details: %s\n\n__Notes: %s' % (
                event.status,
                event.owner(),
                event.type,
                event.estimated_size,
                event.url,
                event.fee,
                event.contact_name,
                event.contact_phone,
                event.roomlist(),
                event.details,
                event.notes)
            # then delete the empty fields with a regex
            ev_desc = re.sub(re.compile(r'^__.*?:[ ,]*$\n*',re.M),'',ev_desc)
            ev_desc = re.sub(re.compile(r'^__',re.M),'',ev_desc)
            ev_url = url_base + event_path(event)
            iev.add('description', ev_desc + '\n--\n' + ev_url)
            iev.add('url', ev_url)
            if event.start_time:
              iev.add('dtstart', event.start_time.replace(tzinfo=pytz.timezone('US/Pacific')))
            if event.end_time:
              iev.add('dtend', event.end_time.replace(tzinfo=pytz.timezone('US/Pacific')))
            cal.add_component(iev)
        return 'text/calendar', cal.as_string()

    def export_rss(self):
        url_base = 'http://' + self.request.headers.get('host', 'events.hackerdojo.com')
        events = Event.get_recent_past_and_future_approved()
        rss = PyRSS2Gen.RSS2(
            title = "Hacker Dojo Events Feed",
            link = url_base,
            description = "Upcoming events at the Hacker Dojo in Mountain View, CA",
            lastBuildDate = datetime.now(),
            items = [PyRSS2Gen.RSSItem(
                        title = "%s @ %s: %s" % (
                            event.start_time.strftime("%A, %B %d"),
                            event.start_time.strftime("%I:%M%p").lstrip("0"),
                            event.name),
                        link = url_base + event_path(event),
                        description = event.details,
                        guid = url_base + event_path(event),
                        pubDate = event.updated,
                        ) for event in events]
        )
        return 'application/xml', rss.to_xml()


class EditHandler(webapp2.RequestHandler):
    def get(self, id):
        event = Event.get_by_id(int(id))
        user = users.get_current_user()
        show_all_nav = user
        access_rights = UserRights(user, event)
        if access_rights.can_edit:
            logout_url = users.create_logout_url('/')
            rooms = ROOM_OPTIONS
            hours = [1,2,3,4,5,6,7,8,9,10,11,12]

            wait_days = _get_user_wait_time(user)

            self.response.out.write(template.render('templates/edit.html', locals()))
        else:
            self.response.out.write("Access denied")

    def post(self, id):
        event = Event.get_by_id(int(id))
        user = users.get_current_user()
        access_rights = UserRights(user, event)
        if access_rights.can_edit:
          try:
            event_times = _validate_event(self, editing_event_id=int(id))
            start_time, end_time = event_times[0]

            other_member = _get_other_member(self, start_time, end_time)
          except ValueError, e:
            error = str(e)
            logging.warning(error)
            self.response.set_status(400)
            self.response.out.write(template.render('templates/error.html', locals()))
            return

          log_desc = ""
          previous_object = Event.get_by_id(int(id))
          event.status = 'pending'
          event.name = self.request.get('name')
          if (previous_object.name != event.name):
            log_desc += "<strong>Title:</strong> " + previous_object.name + " to " + event.name + "<br />"
          event.start_time = start_time
          if (previous_object.start_time != event.start_time):
            log_desc += "<strong>Start time:</strong> " + str(previous_object.start_time) + " to " + str(event.start_time) + "<br />"
          event.end_time = end_time
          if (previous_object.end_time != event.end_time):
            log_desc += "<strong>End time:</strong> " + str(previous_object.end_time) + " to " + str(event.end_time) + "<br />"
          event.estimated_size = cgi.escape(self.request.get('estimated_size'))
          if (previous_object.estimated_size != event.estimated_size):
            log_desc += "<strong>Est. size:</strong> " + previous_object.estimated_size + " to " + event.estimated_size + "<br />"
          event.contact_name = cgi.escape(self.request.get('contact_name'))
          if (previous_object.contact_name != event.contact_name):
            log_desc += "<strong>Contact:</strong> " + previous_object.contact_name + " to " + event.contact_name + "<br />"
          event.contact_phone = cgi.escape(self.request.get('contact_phone'))
          if (previous_object.contact_phone != event.contact_phone):
            log_desc += "<strong>Contact phone:</strong> " + previous_object.contact_phone + " to " + event.contact_phone + "<br />"
          event.details = cgi.escape(self.request.get('details'))
          if (previous_object.details != event.details):
            log_desc += "<strong>Details:</strong> " + previous_object.details + " to " + event.details + "<br />"
          event.url = cgi.escape(self.request.get('url'))
          if (previous_object.url != event.url):
            log_desc += "<strong>Url:</strong> " + previous_object.url + " to " + event.url + "<br />"
          event.fee = cgi.escape(self.request.get('fee'))
          if (previous_object.fee != event.fee):
            log_desc += "<strong>Fee:</strong> " + previous_object.fee + " to " + event.fee + "<br />"
          event.notes = cgi.escape(self.request.get('notes'))
          if (previous_object.notes != event.notes):
            log_desc += "<strong>Notes:</strong> " + previous_object.notes + " to " + event.notes + "<br />"
          event.admin_notes = cgi.escape(self.request.get("admin_notes"))
          if (previous_object.admin_notes != event.admin_notes):
            log_desc += "<strong>Admin Notes:</strong> " + \
                previous_object.admin_notes + " to " + event.admin_notes + "<br />"
          event.rooms = self.request.get_all('rooms')
          if (previous_object.rooms != event.rooms):
            log_desc += "<strong>Rooms changed</strong><br />"
            log_desc += "<strong>Old room:</strong> " + previous_object.roomlist() + "<br />"
            log_desc += "<strong>New room:</strong> " + event.roomlist() + "<br />"
          setup = cgi.escape(self.request.get('setup')) or 0
          event.setup = int(setup)
          if (previous_object.setup != event.setup):
              log_desc += "<strong>Setup time changed</strong><br />"
              log_desc += "<strong>Old time:</strong> %s minutes<br/>" % previous_object.setup
              log_desc += "<strong>New time:</strong> %s minutes<br/>" % event.setup
          teardown = cgi.escape(self.request.get('teardown')) or 0
          event.teardown = int(teardown)
          if (previous_object.teardown != event.teardown):
              log_desc += "<strong>Teardown time changed</strong><br />"
              log_desc += "<strong>Old time:</strong> %s minutes<br/>" % previous_object.teardown
              log_desc += "<strong>New time:</strong> %s minutes<br/>" % event.teardown
          event.other_member = other_member
          if (previous_object.other_member != event.other_member):
            log_desc += "<strong>Other member changed</strong><br />"
            log_desc += "<strong>Old:</strong> %s<br />" % \
                (previous_object.other_member)
            log_desc += "<strong>New:</strong> %s<br />" % \
                (event.other_member)
          log = HDLog(event=event,description="Event edited<br />"+log_desc)
          log.put()
          show_all_nav = user
          access_rights = UserRights(user, event)
          if access_rights.can_edit:
            logout_url = users.create_logout_url('/')
            rooms = ROOM_OPTIONS
            hours = [1,2,3,4,5,6,7,8,9,10,11,12]
            if log_desc:
              edited = "<u>Saved changes:</u><br>"+log_desc
            notify_event_change(event=event,modification=1)
            event.put()
            self.response.out.write(template.render('templates/edit.html', locals()))
          else:
            self.response.set_status(401)
            self.response.out.write("Access denied")
        else:
          self.response.set_status(401)
          self.response.out.write("Access denied")


class EventHandler(webapp2.RequestHandler):
    def get(self, id):
        event = Event.get_by_id(int(id))
        if self.request.path.endswith('json'):
            self.response.headers['content-type'] = 'application/json'
            self.response.out.write(json.dumps(event.to_dict()))
        else:
            user = users.get_current_user()
            if user:
                access_rights = UserRights(user, event)
                logout_url = users.create_logout_url('/')

            else:
                login_url = users.create_login_url('/')
            event.details = db.Text(event.details.replace('\n','<br/>'))
            show_all_nav = user
            event.notes = db.Text(event.notes.replace('\n','<br/>'))

            wait_days = _get_user_wait_time(user)

            self.response.out.write(template.render('templates/event.html', locals()))

    def post(self, id):
        event = Event.get_by_id(int(id))
        user = users.get_current_user()
        action = self.request.get('state')

        _do_event_action(event, action, user)

        event.details = db.Text(event.details.replace('\n','<br/>'))
        show_all_nav = user
        event.notes = db.Text(event.notes.replace('\n','<br/>'))
        self.response.out.write(template.render('templates/event.html', locals()))

class ApprovedHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        today = local_today()
        show_all_nav = user
        events = Event.get_approved_list_with_multiday()
        tomorrow = today + timedelta(days=1)
        whichbase = 'base.html'
        if self.request.get('base'):
            whichbase = self.request.get('base') + '.html'

        wait_days = _get_user_wait_time(user)

        user_rights = UserRights(user)
        is_admin = user_rights.is_admin
        hide_checkboxes = True
        self.response.out.write(template.render('templates/approved.html', locals()))


class MyEventsHandler(webapp2.RequestHandler):
    @util.login_required
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        events = Event.all().filter('member = ', user).order('start_time')
        show_all_nav = user
        today = local_today()
        tomorrow = today + timedelta(days=1)

        wait_days = _get_user_wait_time(user)

        hide_checkboxes = True
        self.response.out.write(template.render('templates/myevents.html', locals()))


class PastHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        today = local_today()
        show_all_nav = user
        events = db.GqlQuery("SELECT * FROM Event WHERE start_time < :1 ORDER" \
                             " BY start_time DESC LIMIT 100", today)

        wait_days = _get_user_wait_time(user)

        self.response.out.write(template.render('templates/past.html', locals()))


class NotApprovedHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        today = local_today()
        tomorrow = today + timedelta(days=1)
        show_all_nav = user
        events = Event.get_recent_not_approved_list()

        wait_days = _get_user_wait_time(user)

        user_rights = UserRights(user)
        is_admin = user_rights.is_admin
        self.response.out.write(template.render('templates/not_approved.html', locals()))


class CronBugOwnersHandler(webapp2.RequestHandler):
    def get(self):
        events = Event.get_pending_list()
        for e in events:
            bug_owner_pending(e)


class AllFutureHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        show_all_nav = user
        events = Event.get_all_future_list()
        today = local_today()
        tomorrow = today + timedelta(days=1)

        wait_days = _get_user_wait_time(user)

        user_rights = UserRights(user)
        is_admin = user_rights.is_admin
        self.response.out.write(template.render('templates/all_future.html', locals()))

class LargeHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        show_all_nav = user
        events = Event.get_large_list()
        today = local_today()
        tomorrow = today + timedelta(days=1)

        wait_days = _get_user_wait_time(user)

        self.response.out.write(template.render('templates/large.html', locals()))


class PendingHandler(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        events = Event.get_pending_list()
        show_all_nav = user
        today = local_today()
        tomorrow = today + timedelta(days=1)

        wait_days = _get_user_wait_time(user)

        user_rights = UserRights(user)
        is_admin = user_rights.is_admin
        self.response.out.write(template.render('templates/pending.html', locals()))


class NewHandler(webapp2.RequestHandler):
    @util.login_required
    def get(self):
        user = users.get_current_user()
        human = human_username(user)
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        rooms = ROOM_OPTIONS
        rules = memcache.get("rules")
        if(rules is None):
          try:
            rules = urlfetch.fetch("http://wiki.hackerdojo.com/api_v2/op/GetPage/page/Event+Policies/_type/html", "GET").content
            memcache.add("rules", rules, 86400)
          except Exception, e:
            rules = "Error fetching rules.  Please report this error to internal-dev@hackerdojo.com."

        to_wait = _get_user_wait_time(user)
        if to_wait:
          # They can't create an event yet.
          error = "You must wait %d days before creating an event." % \
                  (to_wait)
          logging.warning(error)
          self.response.set_status(401)
          self.response.out.write(template.render('templates/error.html', locals()))
          return

        is_admin = UserRights(user).is_admin
        self.response.out.write(template.render('templates/new.html', locals()))

    def post(self):
      # Whether we want to submit the event as a regular member.
      ignore_admin = self.request.get("regular_user", None)
      if ignore_admin:
        logging.info("Validating as regular member.")

      recurring = self.request.get("recurring", None)
      if recurring:
        logging.debug("Submitting recurring event.")

      try:
        event_times = _validate_event(self, ignore_admin=ignore_admin,
                                      recurring=recurring)

        # Since this check is just based on the duration, it doesn't really
        # matter which start and end times we use.
        first_start = event_times[0][0]
        first_end = event_times[0][1]
        other_member = _get_other_member(self, first_start, first_end)
      except ValueError, e:
        error = str(e)
        logging.warning(error)
        self.response.set_status(400)
        self.response.out.write(template.render('templates/error.html', locals()))
        return

      # If we are ignoring our admin status, we are testing, so don't save it.
      if not ignore_admin:
        first_event = None
        for start_time, end_time in event_times:
          event = Event(
              name=cgi.escape(self.request.get('name')),
              start_time=start_time,
              end_time=end_time,
              type=cgi.escape(self.request.get('type')),
              estimated_size=cgi.escape(self.request.get('estimated_size')),
              contact_name=cgi.escape(self.request.get('contact_name')),
              contact_phone=cgi.escape(self.request.get('contact_phone')),
              details=cgi.escape(self.request.get('details')),
              url=cgi.escape(self.request.get('url')),
              fee=cgi.escape(self.request.get('fee')),
              notes=cgi.escape(self.request.get('notes')),
              rooms=self.request.get_all('rooms'),
              expired=local_today() + timedelta(days=PENDING_LIFETIME), # Set expected expiration date
              setup=int(self.request.get('setup') or 0),
              teardown=int(self.request.get('teardown') or 0),
              other_member=other_member,
              admin_notes=self.request.get('admin_notes')
          )

          if not first_event:
            first_event = event

          event.put()
          log = HDLog(event=event,description="Created new event")
          log.put()

        # For obvious reasons, we only notify people about the first event in a
        # recurring series.
        notify_owner_confirmation(first_event)
        notify_event_change(first_event)
      set_cookie(self.response.headers, 'formvalues', None)

      rules = memcache.get("rules")
      if(rules is None):
          try:
              rules = urlfetch.fetch("http://wiki.hackerdojo.com/api_v2/op/GetPage/page/Event+Policies/_type/html", "GET").content
              memcache.add("rules", rules, 86400)
          except Exception, e:
              rules = "Error fetching rules.  Please report this error to internal-dev@hackerdojo.com."
      self.response.out.write(template.render('templates/confirmation.html', locals()))


class ConfirmationHandler(webapp2.RequestHandler):
    def get(self, id):
      event = Event.get_by_id(int(id))
      rules = memcache.get("rules")
      if(rules is None):
          try:
              rules = urlfetch.fetch("http://wiki.hackerdojo.com/api_v2/op/GetPage/page/Event+Policies/_type/html", "GET").content
              memcache.add("rules", rules, 86400)
          except Exception, e:
              rules = "Error fetching rules.  Please report this error to internal-dev@hackerdojo.com."
      user = users.get_current_user()
      logout_url = users.create_logout_url('/')

      wait_days = _get_user_wait_time(user)

      self.response.out.write(template.render('templates/confirmation.html', locals()))

class LogsHandler(webapp2.RequestHandler):
    @util.login_required
    def get(self):
        user = users.get_current_user()
        logs = HDLog.get_logs_list()
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')
        show_all_nav = user

        wait_days = _get_user_wait_time(user)

        self.response.out.write(template.render('templates/logs.html', locals()))

class FeedbackHandler(webapp2.RequestHandler):
    @util.login_required
    def get(self, id):
        user = users.get_current_user()
        event = Event.get_by_id(int(id))
        if user:
            logout_url = users.create_logout_url('/')
        else:
            login_url = users.create_login_url('/')

        wait_days = _get_user_wait_time(user)

        self.response.out.write(template.render('templates/feedback.html', locals()))

    def post(self, id):
        user = users.get_current_user()
        event = Event.get_by_id(int(id))
        try:
            if self.request.get('rating'):
                feedback = Feedback(
                    event = event,
                    rating = int(self.request.get('rating')),
                    comment = cgi.escape(self.request.get('comment')))
                feedback.put()
                log = HDLog(event=event,description="Posted feedback")
                log.put()
                self.redirect('/event/%s-%s' % (event.key().id(), slugify(event.name)))
            else:
                raise ValueError('Please select a rating')
        except Exception:
            set_cookie(self.response.headers, 'formvalues', dict(self.request.POST))
            self.redirect('/feedback/new/' + id)

class TempHandler(webapp2.RequestHandler):
    def get(self):
        units = {"AC1":"EDD9A758", "AC2":"B65D8121", "AC3":"0BA20EDC", "AC5":"47718E38"}
        modes = ["Off","Heat","Cool"]
        master = units["AC3"]
        key = keymaster.get('thermkey')
        url = "https://api.bayweb.com/v2/?id="+master+"&key="+key+"&action=data"
        result = urlfetch.fetch(url)
        if result.status_code == 200:
            thdata = json.loads(result.content)
            inside_air_temp = thdata['iat']
            mode = thdata['mode']
            if inside_air_temp <= 66 and modes[mode] == "Cool":
                for thermostat in units:
                    url = "https://api.bayweb.com/v2/?id="+units[thermostat]+"&key="+key+"&action=set&heat_sp=69&mode="+str(modes.index("Heat"))
                    result = urlfetch.fetch(url)
                notify_hvac_change(inside_air_temp,"Heat")
            if inside_air_temp >= 75 and modes[mode] == "Heat":
                for thermostat in units:
                    url = "https://api.bayweb.com/v2/?id="+units[thermostat]+"&key="+key+"&action=set&cool_sp=71&mode="+str(modes.index("Cool"))
                    result = urlfetch.fetch(url)
                notify_hvac_change(inside_air_temp,"Cold")
            self.response.out.write("200 OK")
        else:
            notify_hvac_change(result.status_code,"ERROR connecting to BayWeb API")
            self.response.out.write("500 Internal Server Error")


""" Expires events that were put on hold when users were suspended. """
class ExpireSuspendedCronHandler(webapp2.RequestHandler):
  def get(self):
    events_query = db.GqlQuery("SELECT * FROM Event WHERE" \
                               " owner_suspended_time != NULL and status = :1",
                               "onhold")

    for event in events_query.run():
      # Check if it's been enough time to expire them.
      expire_period = timedelta(days=Config().SUSPENDED_EVENT_EXPIRY)
      if datetime.now() - event.owner_suspended_time >= expire_period:
        logging.info("Expiring event from suspended user: %s" % (event.name))
        event.expire()


""" Stuff that the bulk action handlers have in common. """
class BulkActionCommon(webapp2.RequestHandler):
  """ Reads the event ids given and produces a list of Event objects.
  Returns: A list of Event objects corresponding to the event ids specified. """
  def _get_events(self):
    event_ids = self.request.get("events")
    event_ids = json.loads(event_ids)

    # Get the actual events.
    events = []
    for event in event_ids:
      events.append(Event.get_by_id(int(event)))

    return events


""" Performs bulk actions on a set of events. """
class BulkActionHandler(BulkActionCommon):
  def post(self):
    action = self.request.get("action")

    events = self._get_events()

    user = users.get_current_user()

    # Perform the action on all the events.
    logging.debug("Performing bulk action: %s" % (action))
    for event in events:
      if not _do_event_action(event, action, user):
        logging.warning("Performing action '%s' failed." % (action))
        self.response.set_status(400)
        return


""" Checks which bulk actions can be performed on a set of events. """
class BulkActionCheckHandler(BulkActionCommon):
  """ Gets a list of bulk actions that can be performed on a set of events.
  Even though this is "safe", it is a POST request because the list of events
  can be extremely long.
  Request parameters:
  events: The list of event ids to check.
  Response: JSON-formatted dictionary containing two lists: A "valid" list of
  valid actions, and an "invalid" list of invalid actions. """
  def post(self):
    events = self._get_events()

    user = users.get_current_user()

    # See what actions can be performed on all the events.
    possible_actions = ["approve", "notapproved", "onhold", "delete"]
    bad_actions = []
    for event in events:
      to_remove = []
      for action in possible_actions:
        if not _do_event_action(event, action, user, check=True):
          # This action cannot be performed.
          bad_actions.append(action)
          to_remove.append(action)

      for action in to_remove:
        possible_actions.remove(action)

    response = {"valid": possible_actions, "invalid": bad_actions}
    self.response.out.write(json.dumps(response))


app = webapp2.WSGIApplication([
        ('/', ApprovedHandler),
        ('/all_future', AllFutureHandler),
        ('/large', LargeHandler),
        ('/pending', PendingHandler),
        ('/past', PastHandler),
        ('/temperature', TempHandler),
        #('/cronbugowners', CronBugOwnersHandler),
        ('/myevents', MyEventsHandler),
        ('/not_approved', NotApprovedHandler),
        ('/new', NewHandler),
        ('/confirm/(\d+).*', ConfirmationHandler),
        ('/edit/(\d+).*', EditHandler),
        # single event views
        ('/event/(\d+).*', EventHandler),
        ('/event/(\d+)\.json', EventHandler),
        # various export methods -- events.{json,rss,ics}
        ('/events\.(.+)', ExportHandler),
        ('/domaincache', DomainCacheCron),
        ('/logs', LogsHandler),
        ('/feedback/new/(\d+).*', FeedbackHandler),
        ('/expire_suspended', ExpireSuspendedCronHandler),
        ('/bulk_action', BulkActionHandler),
        ('/bulk_action_check', BulkActionCheckHandler),
        ],debug=True)
