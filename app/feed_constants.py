"""Constants shared between the feed stack and the admin API.

Single home for the externally visible feed port used by the admin
API's subscription URL (app/admin.py). The actual port mapping lives in
config.yaml (``ports:``) — keep the two in sync.
"""

# Host port the router forwards to the add-on (mapped in config.yaml to
# the feed listener's container port). Part of the public subscription
# URL: https://<host>:8098/feed/<token>.ics
FEED_HOST_PORT = 8098
