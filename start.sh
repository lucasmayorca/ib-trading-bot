#!/bin/sh
exec gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-5000} cloud.server:app
