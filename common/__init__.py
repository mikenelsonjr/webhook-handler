"""The little that both deployables must agree on.

Kept deliberately thin. It holds the log formatter because a trace spanning two
services needs one spelling of ``event_id``, and nothing else: the message
envelope stays pinned by ``tests/contract/`` rather than by a shared constants
module, so neither side can quietly redefine the contract by editing a file it
owns.
"""
