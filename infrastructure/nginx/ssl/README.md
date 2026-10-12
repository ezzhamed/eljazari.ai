Place TLS certificate files here when enabling HTTPS termination at Nginx.

Recommended files:
- fullchain.pem
- privkey.pem

The default nginx.conf is HTTP-only so the container can start even before TLS is provisioned.
Once certificates are present, add a 443 server block and enable ENABLE_HTTPS_REDIRECT=true for the API.
