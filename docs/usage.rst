..
  SPDX-FileCopyrightText: SPDX-FileCopyrightText: 2015 Eric Larson

  SPDX-License-Identifier: Apache-2.0

====================
 Using CacheControl
====================

.. important::

    You **must** read the :doc:`security` page before using CacheControl.

CacheControl assumes you are using a `requests.Session` for your
requests. If you are making ad-hoc requests using `requests.get` then
you probably are not terribly concerned about caching.

There are two way to use CacheControl, via the wrapper and the
adapter.


Wrapper
=======

The easiest way to use CacheControl is to utilize the basic
wrapper. Here is an example: ::

  import requests
  import cachecontrol

  sess = cachecontrol.CacheControl(requests.Session())
  resp = sess.get('http://google.com')

This uses the default cache store, a thread safe in-memory dictionary.


Adapter
=======

The other way to use CacheControl is via a requests `Transport
Adapter`_.

Here is how the adapter works: ::

  import requests
  import cachecontrol

  sess = requests.Session()
  sess.mount('http://', cachecontrol.CacheControlAdapter())

  resp = sess.get('http://google.com')


Under the hood, the wrapper method of using CacheControl mentioned
above is the same as this example.


Use a Different Cache Store
===========================

Both the wrapper and adapter classes allow providing a custom cache
store object for storing your cached data. Here is an example using
the provided `FileCache` from CacheControl: ::

  import requests

  from cachecontrol import CacheControl

  # NOTE: This requires filelock be installed
  from cachecontrol.caches import FileCache

  sess = CacheControl(requests.Session(),
                      cache=FileCache('.webcache'))


The `FileCache` will create a directory called `.webcache` and store a
file for each cached request.


Only-If-Cached
==============

Sending a request with the ``Cache-Control: only-if-cached`` header
tells CacheControl to answer from the cache alone and never touch the
network. This is useful for offline operation: ::

  resp = sess.get('http://example.com',
                  headers={'Cache-Control': 'only-if-cached'})

The cached entry is judged together with the other cache directives:

* ``max-age`` and ``min-fresh`` constrain how fresh the entry must be.
* ``max-stale`` (optionally with a value) allows serving a stale entry,
  unless the cached response carries ``must-revalidate``.
* ``no-cache`` (or ``max-age=0``) on the request demands revalidation,
  which a cache-only lookup cannot perform.

When no usable representation is available, CacheControl returns a
synthetic ``504 Gateway Timeout`` response with ``from_cache`` set to
``True`` (no network request was made). The diagnostics logged by the
``cachecontrol.controller`` logger distinguish a missing cache entry
("no cache entry") from an entry that exists but is not usable (for
example "cache entry is stale and cannot be served"), and are computed
without re-reading the cached response stream.

Redirect chains are subject to the same restriction: every hop of a
redirect is resolved from the cache, and any hop that cannot be
satisfied from the cache results in the same ``504`` response.


.. _Transport Adapter: http://docs.python-requests.org/en/latest/user/advanced/#transport-adapters
