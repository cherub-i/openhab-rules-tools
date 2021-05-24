"""
Copyright June 30, 2020 Richard Koshak

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from core.metadata import get_metadata, get_key_value, get_value
from core.actions import Ephemeris
from core.utils import send_command_if_different
from core.log import log_traceback, logging, LOG_PREFIX
from java.time import ZonedDateTime
from community.time_utils import to_today, to_datetime
from community.timer_mgr import TimerMgr
from community.rules_utils import create_simple_rule, delete_rule, load_rule_with_metadata

# improve typing and linting as per
# https://github.com/CrazyIvan359/openhab-stubs/blob/master/Usage.md
import typing as t
if t.TYPE_CHECKING:  # imports used only for type hints
    from core.jsr223.scope import events, items, UnDefType, DateTimeType


# Name of the Item to trigger reloading of the time of day rule.
ETOD_RELOAD_ITEM = "Reload_ETOD"

# Create the time of day state Item if it doesn't exist.
ETOD_ITEM = "TimeOfDay"
if ETOD_ITEM not in items:
    from core.items import add_item
    add_item(ETOD_ITEM, item_type="String")

# Metadata name space.
NAMESPACE = "etod"

# Timers that run at time of day transitions.
timers = TimerMgr()

# Logger to use before
log = logging.getLogger("{}.Ephemeris Time of Day".format(LOG_PREFIX))

@log_traceback
def check_config(i, log):
    """Verifies that all the required elements are present for an etod metadata."""

    cfg = get_metadata(i, NAMESPACE)
    if not cfg:
        log.error("Item {} does not have {} metadata".format(i, NAMESPACE))
        return None

    if not cfg.value or cfg.value == "":
        log.error("Item {} does not have a value".format(i))
        return None

    day_type = cfg.configuration["type"]

    if not day_type:
        log.error("Item {} does not have a type".format(i))
        return None

    if day_type == "dayset" and not cfg.configuration["set"]:
        log.error("Item {} is of type dayset but doesn't have a set".format(i))
        return None

    elif day_type == "custom" and not cfg.configuration["file"]:
        log.error("Item {} is of type custom but does't have a file".format(i))
        return None

    return cfg

@log_traceback
def get_times():
    """Gets the list of Items that define the start times for today. It uses
    Ephemeris to determine which set of Items to select. The hierarchy is:
        - custom: custom defined holidays
        - holiday: default holidays
        - dayset: custom defined dayset
        - weekend: weekend as defined in Ephemeris
        - weekday: not weekend days
        - default: used when no other day type is detected for today

    Returns:
        - a list of names for DateTime Items; None if no valid start times were
        found.
    """

    def cond(lst, cond):
        return [i for i in lst if cond(i)]

    def types(type):
        return [i for i in items if get_key_value(i, NAMESPACE, "type") == type]

    # Get all of the etod Items that are valid for today.
    start_times = {'default': types("default"),
                   'weekday': types("weekday") if not Ephemeris.isWeekend() else [],
                   'weekend': types("weekend") if Ephemeris.isWeekend() else [],
                   'dayset': cond(types('dayset'),
                                lambda i: Ephemeris.isInDayset(get_key_value(i, NAMESPACE, "set"))),
                   'holiday': types('holiday') if Ephemeris.isBankHoliday() else [], # changed to simpler way of getting holidays
                   'custom': cond(types('custom'),
                                lambda i: Ephemeris.isBankHoliday(0, get_key_value(i, NAMESPACE, "file")))}

    # Determins which start time Items to use according to the hierarchy.
    day_type = None
    if start_times['custom']:
        day_type = 'custom'
    elif start_times['holiday']:
        day_type = 'holiday'
    elif start_times['dayset']:
        day_type = 'dayset'
    elif start_times['weekend']:
        day_type = 'weekend'
    elif start_times['weekday']:
        day_type = 'weekday'
    elif start_times['default']:
        day_type = 'default'

    log.info("Today is a {} day, there are {} time periods today.".format(day_type, len(start_times[day_type])))
    return start_times[day_type] if day_type else None

@log_traceback
def etod_transition(state):
    """Called from the timers, transitions to the next time of day.

    Arguments:
        - state: the state to transition into
    """
    log.info("Transitioning Time of Day from {} to {}"
             .format(items[ETOD_ITEM], state))
    events.sendCommand(ETOD_ITEM, state)

@log_traceback
def create_timers(start_times):
    """Creates Timers to transition the time of day based on the passed in list
    of DateTime Item names. If an Item is dated with yesterday, the Item is
    updated to today. The ETOD_ITEM is commanded to the current time of day if
    it's not already the correct state.

    Arguments:
        - start_times: list of names for DateTime Items containing the start
        times for each time period
    """

    now = ZonedDateTime.now() # changed as DateTime is not available in OH3
    most_recent_time = now.minusDays(1)
    most_recent_state = items[ETOD_ITEM]

    for time in start_times:

        item_time = to_datetime(items[time]) # changed as DateTime is not available in OH3

        trigger_time = to_today(items[time])

        # Update the Item with today's date if it was for yesterday.
        if item_time.isBefore(trigger_time):
            log.debug("Item {} is yesterday, updating to today".format(time))
            events.postUpdate(time, str(DateTimeType(trigger_time))) # changed as DateTime is not available in OH3

        # Get the etod state from the metadata.
        state = get_value(time, NAMESPACE)

        # If it's in the past but after most_recent, update most_recent.
        if trigger_time.isBefore(now) and trigger_time.isAfter(most_recent_time):
            log.debug("NOW:    {} start time {} is in the past but after {}"
                     .format(state, trigger_time, most_recent_time))
            most_recent_time = trigger_time
            most_recent_state = get_value(time, NAMESPACE)

        # If it's in the future, schedule a Timer.
        elif trigger_time.isAfter(now):
            log.debug("FUTURE: {} Scheduleing Timer for {}"
                     .format(state, trigger_time))
            timers.check(state, trigger_time,
                         function=lambda st=state: etod_transition(st))

        # If it's in the past but not after most_recent_time we can ignore it.
        else:
            log.debug("PAST:   {} start time of {} is before now {} and before {}"
                     .format(state, trigger_time, now, most_recent_time))

    log.info("Created {} timers.".format(len(timers.timers)))
    log.info("The current time of day is {}".format(most_recent_state))
    send_command_if_different(ETOD_ITEM, most_recent_state)

def ephem_tod(event):
    """Rule to recalculate the times of day for today. It triggers at system
    start, two minutes after midnight (to give Astro a chance to update the
    times for today), when ETOD_TRIGGER_ITEM (default is CalculateETOD) receives
    an ON command, or when any of the Items with etod metadata changes.
    """
    log.info("Recalculating time of day")

    # Get the start times.
    start_times = get_times()

    if not start_times:
        log.error("No start times found! Cannot run the rule!")
        return

    # If any are NULL, kick off the init rule.
    null_items = [i for i in start_times if isinstance(items[i], UnDefType)]
    if null_items and "InitItems" in items:
        log.warn("The following Items are are NULL/UNDEF, kicking off "
                 "initialization using item_init: {}"
                 .format(null_items))
        events.sendCommand("InitItems", "ON")
        from time import sleep
        sleep(5)

    # Check to see if we still have NULL/UNDEF Items.
    null_items = [i for i in start_times if isinstance(items[i], UnDefType)]
    if null_items:
        log.error("The following Items are still NULL/UNDEF, "
                  "cannot create Time of Day timers: {}"
                  .format(null_items))
        return

    # Cancel existing Items and then generate all the timers for today.
    timers.cancel_all()
    create_timers(start_times)

    # Create a timer to run this rule again a little after midnight. Work around
    # to deal with the fact that cron triggers do not appear to be workind.
    now = ZonedDateTime.now() # changed as DateTime is not available in OH3
    reload_time = now.withHour(0).withMinute(2).withSecond(0).withNano(0) # changed as DateTime is not available in OH3
    if reload_time.isBefore(now):
        reload_time = reload_time.plusDays(1)
        log.info("Creating reload timer for {}".format(reload_time))
    timers.check("etod_reload", reload_time, function=lambda: ephem_tod(None))

@log_traceback
def load_etod(event):
    """Called at startup or when the Reload Ephemeris Time of Day rule is
    triggered, deletes and recreates the Ephemeris Time of Day rule. Should be
    called at startup and when the metadata is added to or removed from Items.
    """

    # Remove the existing rule if it exists.
    if not delete_rule(ephem_tod, log):
        log.error("Failed to delete rule!")
        return None

    # Generate the rule triggers with the latest metadata configs.
    etod_items = load_rule_with_metadata(NAMESPACE, check_config, "changed",
                                         "Ephemeris Time of Day", ephem_tod,
                                         log,
                                         description=("Creates the timers that "
                                                      "drive the {} state"
                                                      "machine".format(ETOD_ITEM)),
                                         tags=["openhab-rules-tools","etod"])
    if etod_items:
        for i in [i for i in timers.timers if not i in etod_items]:
            timers.cancel(i)

    # Generate the timers now.
    ephem_tod(None)

@log_traceback
def scriptLoaded(*args):
    """Create the Ephemeris Time of Day rule."""

    delete_rule(ephem_tod, log)
    if create_simple_rule(ETOD_RELOAD_ITEM, "Reload Ephemeris Time of Day",
            load_etod, log,
            description=("Regenerates the Ephemeris Time of Day rule using the"
                         " latest {0} metadata. Run after adding or removing any"
                         " {0} metadata to/from and Item."
                         .format(NAMESPACE)),
            tags=["openhab-rules-tools","etod"]):
        load_etod(None)


@log_traceback
def scriptUnloaded():
    """Cancel all Timers at unload to avoid errors in the log and removes the
    rules.
    """

    timers.cancel_all()
    delete_rule(ephem_tod, log)
    delete_rule(load_etod, log)
