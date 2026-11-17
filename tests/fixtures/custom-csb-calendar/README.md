# custom-csb-calendar

The source reads the public Google Calendar of the Department of Chemical and Systems
Biology (adapter `ics`).

- `department-special-programs-events-*.html`: recorded 2026-11-17 from
  https://chemsysbio.stanford.edu/department/special-programs-events/, the page that
  embeds the calendar (`iframe`, `src=csbdept@gmail.com`).
- `calendar-ical-csbdept-gmail-com-public-*.ics`: recorded 2026-11-17 from
  https://calendar.google.com/calendar/ical/csbdept%40gmail.com/public/basic.ics, with
  the robots.txt of that host ("Allow: /$", "Disallow: /") set aside as the source's
  `ignore_robots` says. **Trimmed:** the answer held every event since 2009 (1213
  VEVENTs, 578692 bytes); the file keeps the calendar header with its VTIMEZONE and the
  31 VEVENTs that start on 2026-10-01 or later, unchanged and in their order. The
  VEVENTs left out include the Zoom details (meeting id, password, dial-in numbers) and
  the personal addresses of older announcements.
- The kept events, 2026-10-13 to 2027-06-01: the Science Circle talks (a "Pizza Talk"
  by a student or a "Faculty Talk"; DESCRIPTION "Speaker: Name - Lab" and a moderator
  line, or "Speaker: TBA"), the CSB Special Seminars of David Garfield (2026-11-16, an
  HTML description with "Talk Title:" and "Abstract:"), Stephan Sieber (2026-12-03, no
  description) and Jeanne Hardy (2027-02-08, "Faculty Host: ..."), and social events
  (Friendsgiving Lunch, Holiday Party). The UIDs end in `@google.com`; they are no
  addresses.
