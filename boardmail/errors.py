"""One entry for each error code: its next-step hint, its exit code and, through that, its MCP error flag.

A code stays a plain string where it is raised. This module imports nothing
from the package, so every module can import it at the top. That is why the
error itself and the checks of a value that every module shares are here too.
"""
from typing import NamedTuple
from uuid import UUID


class Entry(NamedTuple):
    next_action: str = 'retry_collect'  # Entry() is a code with no hint of its own.
    exit_code: int = 2


# A code a custom adapter reports, or an HTTP status that has no entry.
UNLISTED = Entry()
# MCP flags a result as an error by its exit code. Every error code exits 2 or 5.
# Exit 1 is no error code: a partial collection, a failed freshness check,
# incomplete context or an unverified reply.
MCP_ERROR_EXIT_CODES = (1, 2, 5)

CODES = {
    'account_mismatch': Entry('restore_source_identity_or_use_a_new_source'),
    'adapter_failed': Entry('check_trusted_adapter_code'),
    'adapter_load_failed': Entry('check_trusted_adapter_code'),
    'adapter_mismatch': Entry('restore_source_identity_or_use_a_new_source'),
    'adapter_version_unsupported': Entry('check_trusted_adapter_code'),
    'budget_exhausted': Entry(),
    'config_missing': Entry('check_config_and_credentials', 5),
    'credentials_unavailable': Entry('check_config_and_credentials'),
    'database_exists': Entry('use_existing_database_do_not_overwrite'),
    'database_missing': Entry('run_init', 5),
    'invalid_adapter_result': Entry('check_trusted_adapter_code'),
    'invalid_arguments': Entry('check_command_help_and_returned_message_ids'),
    'invalid_config': Entry('check_config_and_credentials'),
    'invalid_mark': Entry('check_command_help_and_returned_message_ids'),
    'invalid_message_id': Entry('check_command_help_and_returned_message_ids'),
    'invalid_reply_body': Entry('use_nonempty_utf8_text_up_to_65536_bytes'),
    'invalid_request': Entry(),
    'invalid_response': Entry(),
    'invalid_settings': Entry('run_settings_reset'),
    'invalid_tag_name': Entry('use_1_to_64_lowercase_letters_digits_hyphens_or_underscores_starting_with_a_letter_or_digit'),
    'invalid_thread_id': Entry('use_a_thread_uuid_from_a_message_or_board'),
    'invalid_totp_secret': Entry('check_totp_secret_and_system_clock'),
    'message_not_found': Entry('check_command_help_and_returned_message_ids'),
    'network_error': Entry(),
    'original_deleted': Entry(),
    'original_incomplete': Entry(),
    'original_unavailable': Entry(),
    'pagination_no_progress': Entry(),
    'pending_overflow': Entry(),
    'redirect_refused': Entry(),
    'reply_adapter_identity_unknown': Entry('restore_source_identity_before_verifying'),
    'reply_already_recorded': Entry('inspect_saved_reply_do_not_publish_again'),
    'reply_already_started': Entry('inspect_saved_reply_do_not_publish_again'),
    'reply_author_mismatch': Entry(),
    'reply_body_conflict': Entry('show_saved_reply_before_changing_a_draft'),
    'reply_candidate_limit': Entry('inspect_saved_candidates_or_use_independent_readback_and_reply_confirm'),
    'reply_identity_mismatch': Entry(),
    'reply_incomplete': Entry(),
    'reply_key_mismatch': Entry('show_saved_reply_before_changing_a_draft'),
    'reply_not_prepared': Entry('prepare_reply_before_publishing'),
    'reply_not_started': Entry('begin_before_publishing'),
    'reply_not_visible': Entry(),
    'reply_provider_not_verified': Entry(),
    'reply_provider_status_unknown': Entry(),
    'reply_readback_mismatch': Entry('reconcile_publication_before_confirming'),
    'reply_ref_required': Entry('check_command_help_and_returned_message_ids'),
    'reply_reference_conflict': Entry('reconcile_publication_before_confirming'),
    'reply_reference_unsupported': Entry('supply_exact_reply_url_on_the_configured_board'),
    'reply_target_mismatch': Entry(),
    'reply_thread_mismatch': Entry(),
    'reply_verification_unsupported': Entry('use_independent_readback_and_reply_confirm'),
    'response_too_large': Entry(),
    'source_not_found': Entry('check_source_name_in_status_or_config'),
    'source_paused': Entry('inspect_source_pause_before_remote_verification'),
    'source_timeout': Entry(),
    'subscription_config_required': Entry('rerun_with_config_to_identify_source_adapter'),
    'subscriptions_unsupported': Entry('use_a_source_with_subscription_support'),
    'thread_deleted': Entry(),
    'unsupported_database': Entry('inspect_database_do_not_delete'),

    # The codes below are built at run time or reported without a raise, so no
    # raise holds them as a literal to search for.
    # A Colony sign-in refusal, from providers.colony_auth_error.
    'auth_2fa_invalid': Entry('check_totp_secret_and_system_clock'),
    'auth_2fa_required': Entry('configure_colony_totp_secret_file'),
    'auth_agent_only': Entry('check_config_and_credentials'),
    'auth_invalid_token': Entry('check_config_and_credentials'),
    'auth_ip_denied': Entry('check_config_and_credentials'),
    'auth_pending_activation': Entry('check_config_and_credentials'),
    'auth_token_revoked': Entry('check_config_and_credentials'),
    # The HTTP statuses that have a hint of their own.
    'http_401': Entry('check_config_and_credentials'),
    'http_403': Entry('check_config_and_credentials'),
    'http_429': Entry('wait_before_collecting_again'),
    # Another collector saved the source first, from boards.collect_all.
    'collection_conflict': Entry(),
    # A local file or database failure, from commands and the MCP start.
    'local_state_error': Entry('inspect_database_do_not_delete'),
    # What the 4claw and Fruitflies adapters report as a batch error.
    'fourclaw_http_error': Entry(),
    'fourclaw_invalid_public_page': Entry(),
    'fourclaw_network_error': Entry(),
    'network_timeout': Entry(),
    # What reply verification builds from a Moltbook lookup.
    'hidden_by_provider': Entry(),
    'reply_deleted': Entry(),
    'reply_missing': Entry(),
    'thread_missing': Entry(),
}


class MailError(Exception):
    """A fixed safe error code, never provider prose, credentials or paths."""


def uuid(value):
    return str(UUID(value))


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid identifier")
    value.encode("utf-8")
    return value


def converted(convert, *values, error=None, otherwise=None):
    """convert(*values). Where convert does not take them, the error code is raised if there is one, and
    otherwise is the answer if there is none."""
    try:
        return convert(*values)
    except (ValueError, TypeError, AttributeError):
        if error is None:
            return otherwise
        raise MailError(error) from None


def next_action(code):
    return CODES.get(code, UNLISTED).next_action


def exit_code(code):
    return CODES.get(code, UNLISTED).exit_code


def mcp_error(exit_status):
    return exit_status in MCP_ERROR_EXIT_CODES
