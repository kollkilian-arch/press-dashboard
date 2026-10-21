# VersicherungsJournal certificate chain

`starfield-r1-cross-g2.pem` is the public **Starfield TLS Root CA - R1 to G2
Cross Certificate**, issued by **Starfield Root Certificate Authority - G2**.

- Source: https://certs.starfieldtech.com/repository/sf_tls_root-r1-cross-g2.crt
- SHA-256: `26A93D60039077654C0DDA1FB634AF5FF5C114C7AC1FD0449B1C9D2178B028FF`
- Downloaded and verified against Requests' existing CA bundle on 2026-10-21.

VersicherungsJournal currently serves its leaf and R1 issuing intermediate but
omits this cross certificate. `text_fetcher.py` appends it to a temporary copy of
Requests' standard CA bundle only for HTTPS URLs on `versicherungsjournal.de`
and `www.versicherungsjournal.de`, including the browser-compatible retry.
Certificate and hostname verification stay enabled. No certificate download is
needed at runtime, and the temporary bundle is deleted after each request.

The publisher should serve the full chain. Once that is fixed, this workaround
can be removed after checking both hostnames with the standard Requests client.
