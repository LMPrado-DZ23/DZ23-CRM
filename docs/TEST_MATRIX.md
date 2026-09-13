# Matriz de testes — prompt mestre §15

Cada item obrigatório da Fase 11 aponta para o(s) teste(s) que o comprovam. Todos
rodam com a tag `dz23`, sem rede e sem credenciais reais (APIs simuladas).

- **Evidência local:** `bash scripts/smoke.sh` → `0 failed, 0 error(s) of 264 tests`
  (banco descartável `dz23_smoke_20117`, instalação limpa + upgrade `-u`), após as
  correções da auditoria final ([`audit/FINAL_THREE_AGENT_REVIEW.md`](../audit/FINAL_THREE_AGENT_REVIEW.md)).
- **Evidência remota:** job `odoo-tests` do GitHub Actions no mesmo commit (mesmos
  gates: módulos carregados, zero falhas/erros, nenhuma linha ERROR/CRITICAL e
  atualização `-u` sem erro).

Convenção: `módulo/arquivo::teste` (arquivos em `addons_custom/<módulo>/tests/`).

## Webhooks

| Item | Testes |
|---|---|
| Assinatura ausente | `dz23_whatsapp/test_webhook_auth::test_missing_header_401`; `test_webhook_providers::test_twilio_missing_or_wrong_signature_401`; `dz23_payment_woovi/test_woovi_webhook::test_missing_or_invalid_signature_is_401` |
| Assinatura inválida | `test_webhook_auth::test_wrong_secret_401`; `test_webhook_providers::test_meta_invalid_signature_is_401`; `test_woovi_webhook::test_missing_or_invalid_signature_is_401` |
| Corpo grande | `test_webhook_limits::test_evolution_body_over_1mib_is_413`; `test_woovi_webhook::test_large_body_is_413`; lote acima de 1000 eventos: `test_webhook_limits::test_meta_batch_over_limit_is_413_and_nothing_is_stored` |
| JSON inválido | `test_webhook_auth::test_invalid_json_400`; `test_webhook_limits::test_meta_invalid_json_with_valid_signature_is_400`; `test_woovi_webhook::test_invalid_json_is_400` |
| Canal inexistente | `test_webhook_auth::test_unknown_token_404`; `test_webhook_providers::test_twilio_unknown_token_404` |
| Canal divergente | `test_webhook_auth::test_cross_channel_secret_isolation`; `test_webhook_auth::test_admin_key_is_not_callback_secret_401` |
| Instância divergente | `test_webhook_auth::test_wrong_instance_409` |
| Telefone divergente | `test_webhook_providers::test_meta_foreign_phone_number_id_is_409`; `test_webhook_providers::test_twilio_foreign_account_or_number_is_409` |
| Replay | `test_webhook_auth::test_redelivery_is_deduplicated`; `test_woovi_webhook::test_valid_signature_persists_event_once` |
| Evento duplicado | `test_message_lifecycle::test_duplicate_callback_is_deduplicated`; `test_woovi::test_duplicate_event_has_single_effect` |
| Evento fora de ordem | `test_message_lifecycle::test_late_event_does_not_regress`; `test_woovi::test_out_of_order_expired_after_completed_is_ignored` |
| Extras | credencial ausente → 503: `test_webhook_auth::test_meta_no_app_secret_503`, `test_webhook_providers::test_twilio_without_credentials_503`; falha ao persistir → 500 (reentrega): `test_webhook_auth::test_persist_failure_returns_500_for_redelivery`; evento fora do contrato: `test_webhook_providers::test_invalid_event_is_skipped_not_fatal` |

## Mensagens

| Item | Testes |
|---|---|
| Inbound duplicado | `test_inbox::test_enqueue_dedupe_sequential`; `test_webhook_providers::test_twilio_inbound_is_persisted_once` |
| Outbound duplicado | `test_outbox::test_cron_sends_once_no_duplicate`; `test_queue_claim::test_concurrent_claim_is_disjoint` |
| Timeout depois do envio | `test_queue_claim::test_outbox_expired_lease_with_provider_id_is_not_resent`; `test_queue_claim::test_outbox_expired_lease_without_id_retries`; `test_provider_errors::test_meta_callback_reconciles_lost_ack` |
| Status antes do commit do envio | `test_outbox::test_status_callback_before_send_commit_is_applied` |
| Retry | `test_outbox::test_process_failure_retries_then_dlq`; `test_inbox::test_retry_backoff_then_dlq`; `test_provider_errors::test_network_error_is_transient` |
| `Retry-After` | `test_provider_errors::test_rate_limit_respects_retry_after_and_pauses_channel`; `test_provider_errors::test_retry_after_http_date` |
| DLQ | `test_provider_errors::test_permanent_error_goes_straight_to_dlq`; `test_queue_claim::test_inbox_expired_lease_at_max_attempts_goes_to_dlq`; `test_message_lifecycle::test_dlq_sets_failed_lifecycle` |
| Requeue | `test_outbox::test_requeue_from_dlq`; `test_inbox::test_requeue_from_dlq` |
| Status monotônico | `test_message_lifecycle::test_forward_progression`, `test_late_event_does_not_regress`, `test_failure_after_delivered_records_error_only`, `test_unknown_never_changes_status` |
| Provider ID ausente | `test_outbox::test_provider_accept_without_message_id_is_not_sent`; `test_message_lifecycle::test_contract_validation`; `test_queue_claim::test_enqueue_requires_message_id` |
| Payload grande | `test_queue_claim::test_large_payload_is_stored_whole_and_processed` |
| Mídia inválida | `test_media::test_html_disguised_as_image_is_rejected`, `test_executable_and_blocked_extension_are_rejected`, `test_size_limit`, `test_meta_url_outside_allowlist_is_rejected`, `test_meta_media_id_path_injection_is_rejected` |

## Agente

| Item | Testes |
|---|---|
| Pergunta de preço não cria pedido | `dz23_agent/test_agent::test_price_does_not_create_order`; `test_agent_flows::test_price_question_never_creates_order` |
| Compra repetida não duplica orçamento | `test_agent_flows::test_repeated_purchase_reuses_open_quote` |
| Confirmação explícita | `test_agent::test_buy_requires_explicit_confirmation`; `test_agent_flows::test_confirmation_is_idempotent_for_same_token`, `test_negative_answer_cancels_pending`, `test_expired_pending_is_not_confirmed` |
| Produto ambíguo | `test_agent::test_buy_ambiguous_asks_no_order`; `test_agent_flows::test_variant_must_be_chosen` |
| Dupla reserva | `test_agent::test_conflict_no_double_booking`; `test_agent_flows::test_concurrent_booking_is_serialized`, `test_retry_with_same_correlation_creates_single_event` |
| Fuso horário | `test_agent_flows::test_timezone_is_stored_in_utc` |
| Feriado | `test_agent_flows::test_holiday_rejected` |
| Fora do expediente | `test_agent_flows::test_outside_business_hours_and_weekend_rejected` |
| Handoff humano | `test_agent_governance::test_sensitive_subject_hands_off_to_human`, `test_free_text_turn_limit_hands_off`; `test_conversation_bot::test_bot_is_silent_when_human_is_active` |
| Falha de IA | `test_ai_queue::test_failure_falls_back_after_max_attempts`; `test_agent_governance::test_circuit_open_hands_off_without_retry`; `dz23_ai/test_ai_governance::test_rate_limit_opens_breaker_and_blocks_next_call` |

## PIX (Woovi)

| Item | Testes |
|---|---|
| Renderização repetida não cria nova cobrança | `test_woovi::test_repeated_render_creates_single_charge` |
| Evento duplicado | `test_woovi::test_duplicate_event_has_single_effect`; `test_woovi_webhook::test_valid_signature_persists_event_once` |
| Valor divergente | `test_woovi::test_value_mismatch_is_rejected` |
| Valor ausente | `test_woovi::test_missing_value_does_not_confirm` |
| Moeda incorreta | `test_woovi::test_currency_mismatch_is_rejected`; `test_woovi::test_only_brl_is_accepted` |
| Cobrança expirada | `test_woovi::test_expired_charge_is_not_recreated`; `test_woovi::test_expired_event_cancels_pending` |
| Pagamento confirmado | `test_woovi::test_completed_with_value_confirms` |
| Estorno | `test_woovi::test_refund_is_recorded` |
| Webhook fora de ordem | `test_woovi::test_out_of_order_expired_after_completed_is_ignored` |
| Webhook forjado por outra conta Woovi | `test_woovi::test_webhook_status_comes_from_api_not_payload`, `test_webhook_for_another_charge_is_rejected` |

## Multi-tenant

| Item | Testes |
|---|---|
| Empresa A não vê empresa B | `test_tenancy::test_search_hides_other_company`, `test_read_other_company_denied`; `test_conversation::test_attendant_is_isolated_by_company` |
| Canal A não acessa credenciais B | `test_webhook_auth::test_cross_channel_secret_isolation`; `test_tenancy::test_token_uniqueness` |
| Lead A não é associado ao canal B | `dz23_agent/test_tenancy_agent::test_same_phone_creates_one_lead_per_company`; `test_tenancy::test_same_phone_suffix_distinct_identities` |
| Calendário isolado | `test_agent::test_conflict_isolated_by_company` |
| Pedido isolado | `test_tenancy_agent::test_sale_order_is_isolated_by_company` |
| Pagamento isolado | `dz23_payment_woovi/test_woovi_tenancy::test_payment_and_event_hidden_from_other_company` |
| Métricas isoladas | `test_health_metrics::test_metrics_isolated_by_company` |
| DLQ isolada | `test_tenancy_queues::test_dlq_search_is_isolated_by_company`, `test_reading_other_company_dlq_is_denied` |
| Dados não mudam de empresa | `test_tenancy::test_contact_channel_cannot_move_to_other_company`; contatos/composição só Atendente: `test_conversation::test_plain_user_cannot_read_contacts_or_compose` |
| Extras | limites de IA por empresa: `test_ai_governance::test_daily_call_limit_per_company`; LGPD por empresa: `test_privacy::test_subject_hash_is_per_company_and_stable` |

## Instalação

| Item | Evidência |
|---|---|
| Instalação limpa | `scripts/smoke.sh` e job `odoo-tests` (instala os 9 módulos do zero, `--without-demo=all`) |
| Atualização de módulo | passo `-u` do `scripts/smoke.sh` e passo "Atualização (-u)" do CI, sobre o banco já instalado |
| Migração | `test_migrations::test_19_0_8_backfills_old_rows`; `test_migrations::test_every_migration_script_loads` |
| Rollback documentado | [`runbooks/backup_restore.md`](runbooks/backup_restore.md) §5 |
| Banco vazio | instalação limpa acima |
| Banco com dados antigos | upgrade do banco de desenvolvimento `dz23crm` (dados reais de teste acumulados desde a baseline) a cada fase, com `pg_dump` prévio — registrado em `audit/AUTONOMOUS_MISSION_STATE.md`; `test_migrations` cobre o preenchimento de linhas antigas |

## Concorrência (além do §15)
`test_queue_claim::test_concurrent_claim_is_disjoint`,
`test_business_action::test_concurrent_same_key_waits_then_reuses`,
`test_agent_flows::test_concurrent_booking_is_serialized` — usam cursores reais
separados para provar `FOR UPDATE SKIP LOCKED` e locks consultivos.
