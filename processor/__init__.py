"""The Pub/Sub push subscriber.

Pub/Sub POSTs each message to an HTTPS endpoint here, so a push body is plain
JSON over HTTP and this package imports nothing from ``google``: no grpc
toolchain, no credentials, and a suite that runs without the ``[gcp]`` extra.

Ingest's rule is *a 2xx is a promise*. This side is the same sentence read from
the other end — **a 2xx means "never send me this again"** — because in push
delivery the response status *is* the acknowledgement. See CONTEXT.md.
"""
