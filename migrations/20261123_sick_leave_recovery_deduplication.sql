-- Prevent concurrent recovery jobs from queueing the same notification.
create unique index if not exists sick_leave_one_recovery_event
    on public.sick_leave_events (sick_leave_id)
    where event_type = 'recovered';
