# Diabase

*Leggi in: [English](README.md)*

**Control plane agent-safe per Supabase, self-hosted o cloud.**

Diabase permette ad agenti AI (Claude Code, o qualsiasi LLM via API) di operare e gestire l'intero backend Supabase — schema, edge functions, workflow di autenticazione, storage — mentre tu osservi ogni mossa da una GUI moderna e resti padrone delle decisioni che contano.

> ⚠️ **Stato: sviluppo iniziale.** Niente di ciò che c'è qui è pronto per la produzione. Segui il progetto o metti una stella — il primo rilascio usabile verrà annunciato.

## Perché

L'MCP ufficiale di Supabase esegue direttamente sul tuo progetto di produzione: niente piano, niente diff, niente approvazione, niente audit trail. Diabase è il layer con opinione che rende la gestione del backend via agente abbastanza sicura da poter essere permessa in azienda:

- **Plan & Approve** — le operazioni grandi diventano un piano leggibile che approvi, rifiuti o correggi; i passi distruttivi sono evidenziati
- **Audit trail** — ogni azione, umana o AI, registrata append-only
- **Adapters** — la stessa esperienza su Supabase self-hosted o Supabase Cloud
- **Progetti** — workspace stile Claude con system prompt, file di contesto e livello di autonomia propri
- **Il tuo LLM, la tua scelta** — Claude Code CLI (abbonamento) o API a consumo, configurato dalle impostazioni

## Licenza

[Apache 2.0](LICENSE)
