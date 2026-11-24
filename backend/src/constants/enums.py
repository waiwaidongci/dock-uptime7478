from enum import Enum


class Role(str, Enum):
    analyst = 'analyst'
    reviewer = 'reviewer'
    viewer = 'viewer'


class State(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'


class AmendmentAction(str, Enum):
    reschedule = '改期'
    cancel = '撤销'


class ReissueState(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    ready = 'ready'
    failed = 'failed'
    returned = 'returned'
    approved = 'approved'
