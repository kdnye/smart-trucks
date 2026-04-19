# FSI Ecosystem Reference

This system is part of the FSI internal tools ecosystem. All FSI apps share a single GCP Cloud SQL PostgreSQL instance.

## This System's Role

Edge telematics hardware deployed on Raspberry Pi devices in FSI vehicles. Collects GPS, BLE temperature/humidity, and power management data and syncs to the shared FSI PostgreSQL database. This is an **approved architectural exception** (Balena edge containers, not a Flask web app).

This system is a data **producer only** — it writes telematics rows to tables owned by `kdnye/motive-dashboard` and never runs structural DB migrations.

## Canonical Ecosystem Document

Full app portfolio, shared DB schema ownership, cross-app data flows, and future roadmap:

→ **[FSI_ECOSYSTEM.md in kdnye/lifecycle](https://github.com/kdnye/lifecycle/blob/main/FSI_ECOSYSTEM.md)**

## Governance Handbook

Complete technical standards (where applicable to edge computing context):

→ **[FSI Application Architecture Standard](https://github.com/kdnye/lifecycle/blob/main/FSI%20Application%20Architecture%20Standard%3A%20Technical%20Governance%20Handbook)**

> When a dedicated `kdnye/fsi-docs` repository is created, update these links to point there.
