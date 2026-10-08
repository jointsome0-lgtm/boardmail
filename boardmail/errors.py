"""One entry for each error code: its next-step hint, the call that is that step where it is one call, its exit
code and, through that, its MCP error flag.

A hint names a step that can work. retry_collect is the hint of a pass that could not finish, and of a code
without an entry.

A code stays a plain string where it is raised. This module imports nothing
from the package, so every module can import it at the top. That is why the
error itself, the checks of a value that every module shares and the shape
of a call that a result names are here too.
"""
from typing import NamedTuple
from uuid import UUID


class Call(NamedTuple):
    """The call that is the next step after an error."""
    command: str
    takes: tuple = ()  # the arguments that it has from the call that failed
    more: tuple = ()   # its other arguments, as pairs of a name and a value


class Entry(NamedTuple):
    next_action: str = 'retry_collect'  # Entry() is a code of a pass that could not finish. The next one can.
    exit_code: int = 2
    next: Call = None  # the call that is the next step, where the step is one call


# The next step after a reply command that was refused for what the journal holds: read the journal.
JOURNAL = Call('reply_show', ('source', 'id'))
# How long a value may be that a next call has from the call that failed: what a tool takes for that argument.
LONGEST = {'source': 64, 'id': 1024}


# A code a custom adapter reports, or an HTTP status that has no entry.
UNLISTED = Entry()
# MCP flags a result as an error by its exit code. Every error code exits 2 or 5.
# Exit 1 is no error code: a partial collection, a failed freshness check,
# incomplete context or an unverified reply.
MCP_ERROR_EXIT_CODES = (1, 2, 5)

# Three hints are for an outcome that another pass does not change:
# continue_without_the_original: an original or a thread is not there to read on its board, or not whole.
# reconcile_publication_before_retry: what a board shows of a reply does not prove that it is ours and in its
# place. A check of a reply that failed has this hint in its result as well.
# report_to_the_operator: the board answers in a way that this package does not take, whenever it is asked.
CODES = {
    'account_mismatch': Entry('restore_source_identity_or_use_a_new_source'),
    'adapter_failed': Entry('check_trusted_adapter_code'),
    'adapter_load_failed': Entry('check_trusted_adapter_code'),
    'adapter_mismatch': Entry('restore_source_identity_or_use_a_new_source'),
    'adapter_version_unsupported': Entry('check_trusted_adapter_code'),
    'budget_exhausted': Entry(),
    'config_missing': Entry('check_config_and_credentials', 5),
    'credentials_unavailable': Entry('check_config_and_credentials'),
    'database_exists': Entry('use_existing_database_do_not_overwrite', next=Call('status')),
    'database_missing': Entry('run_init', 5, Call('init')),
    'invalid_adapter_result': Entry('check_trusted_adapter_code'),
    'invalid_arguments': Entry('fix_the_arguments'),
    'invalid_config': Entry('check_config_and_credentials'),
    'invalid_mark': Entry('give_ref_only_with_action_replied'),
    'invalid_message_id': Entry('use_the_exact_id_of_a_returned_message'),
    'invalid_reply_body': Entry('use_nonempty_utf8_text_up_to_65536_bytes'),
    'invalid_request': Entry('report_to_the_operator'),
    'invalid_response': Entry(),
    'invalid_settings': Entry('run_settings_reset', next=Call('settings', more=(('reset', True),))),
    'invalid_tag_name': Entry('use_1_to_64_lowercase_letters_digits_hyphens_or_underscores_starting_with_a_letter_or_digit'),
    'invalid_thread_id': Entry('use_a_thread_uuid_from_a_message_or_board'),
    'invalid_totp_secret': Entry('check_totp_secret_and_system_clock'),
    'message_not_found': Entry('use_the_source_and_id_of_a_listed_message',
                               next=Call('list', ('source',), (('scope', 'all'), ('context', 'none')))),
    'network_error': Entry(),
    'original_deleted': Entry('continue_without_the_original'),
    'original_incomplete': Entry('continue_without_the_original'),
    'original_unavailable': Entry('continue_without_the_original'),
    'pagination_no_progress': Entry(),
    'pending_overflow': Entry(),
    'redirect_refused': Entry('report_to_the_operator'),
    'reply_adapter_identity_unknown': Entry('restore_source_identity_before_verifying'),
    'reply_already_recorded': Entry('inspect_saved_reply_do_not_publish_again', next=JOURNAL),
    'reply_already_started': Entry('inspect_saved_reply_do_not_publish_again', next=JOURNAL),
    'reply_author_mismatch': Entry('reconcile_publication_before_retry'),
    'reply_body_conflict': Entry('show_saved_reply_before_changing_a_draft', next=JOURNAL),
    'reply_candidate_limit': Entry('inspect_saved_candidates_or_use_independent_readback_and_reply_confirm', next=JOURNAL),
    'reply_identity_mismatch': Entry('reconcile_publication_before_retry'),
    'reply_incomplete': Entry('reconcile_publication_before_retry'),
    'reply_key_mismatch': Entry('show_saved_reply_before_changing_a_draft', next=JOURNAL),
    'reply_not_prepared': Entry('prepare_reply_before_publishing', next=JOURNAL),
    'reply_not_started': Entry('begin_before_publishing', next=JOURNAL),
    'reply_not_visible': Entry('reconcile_publication_before_retry'),
    'reply_provider_not_verified': Entry('reconcile_publication_before_retry'),
    'reply_provider_status_unknown': Entry('reconcile_publication_before_retry'),
    'reply_readback_mismatch': Entry('reconcile_publication_before_confirming', next=JOURNAL),
    'reply_ref_required': Entry('supply_the_url_of_the_published_reply_as_ref'),
    'reply_reference_conflict': Entry('reconcile_publication_before_confirming', next=JOURNAL),
    'reply_reference_unsupported': Entry('supply_exact_reply_url_on_the_configured_board'),
    'reply_target_mismatch': Entry('reconcile_publication_before_retry'),
    'reply_thread_mismatch': Entry('reconcile_publication_before_retry'),
    'reply_verification_unsupported': Entry('use_independent_readback_and_reply_confirm'),
    'response_too_large': Entry('report_to_the_operator'),
    'source_not_found': Entry('check_source_name_in_status_or_config', next=Call('status')),
    'source_paused': Entry('inspect_source_pause_before_remote_verification', next=Call('status')),
    'source_timeout': Entry(),
    'subscription_config_required': Entry('rerun_with_config_to_identify_source_adapter'),
    'subscriptions_unsupported': Entry('use_a_source_with_subscription_support'),
    'thread_deleted': Entry('continue_without_the_original'),
    'unsupported_database': Entry('inspect_database_do_not_delete'),

    # The codes below are built at run time or reported without a raise, so no
    # raise holds them as a literal to search for.
    # A Colony sign-in refusal, from adapter_colony.colony_auth_error.
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
    # A local file or database failure, from commands and the MCP start. Only a reply command gives the result
    # what it was called with, so only its failure names the read of the journal.
    'local_state_error': Entry('inspect_database_do_not_delete', next=JOURNAL),
    # What the 4claw and Fruitflies adapters report as a batch error.
    'fourclaw_http_error': Entry(),
    'fourclaw_invalid_public_page': Entry(),
    'fourclaw_network_error': Entry(),
    'network_timeout': Entry(),
    # What reply verification builds from a Moltbook lookup.
    'hidden_by_provider': Entry('continue_without_the_original'),
    'reply_deleted': Entry('reconcile_publication_before_retry'),
    'reply_missing': Entry('reconcile_publication_before_retry'),
    'thread_missing': Entry('continue_without_the_original'),
}


class MailError(Exception):
    """A fixed safe error code, never provider prose, credentials or paths.

    argument is the name that the command table has for the argument whose value was refused. It is None where
    the error is about no single argument, and it is never a word of the caller. call is what the command that
    failed was given, where a command ran: commands.execute() sets it for the next step that the error names."""

    def __init__(self, code, *, argument=None):
        super().__init__(code)
        self.argument, self.call = argument, None


def uuid(value):
    return str(UUID(value))


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid identifier")
    value.encode("utf-8")
    return value


def converted(convert, *values, error=None, otherwise=None, argument=None):
    """convert(*values). Where convert does not take them, the error code is raised if there is one, and
    otherwise is the answer if there is none. argument is the argument that the values are of, for the error."""
    try:
        return convert(*values)
    except (ValueError, TypeError, AttributeError):
        if error is None:
            return otherwise
        raise MailError(error, argument=argument) from None


def next_action(code):
    return CODES.get(code, UNLISTED).next_action


def route(command, **arguments):
    """A call that a result names, in the one shape that every result writes it in: the tool, and its arguments.
    On the command line the tool boardmail_reply_show is the command reply show."""
    return {'tool': 'boardmail_' + command, 'arguments': arguments}


def following(code, call):
    """The call that is the next step after an error, as a route. call is what the command that failed was
    given. None where the code has no such step, and where the call has no name or id that a tool takes to give
    the step."""
    step = CODES.get(code, UNLISTED).next
    if step is None or not all(converted(identifier, call.get(name)) and len(call[name]) <= LONGEST[name]
                               for name in step.takes):
        return None
    return route(step.command, **{name: call[name] for name in step.takes}, **dict(step.more))


def exit_code(code):
    return CODES.get(code, UNLISTED).exit_code


def mcp_error(exit_status):
    return exit_status in MCP_ERROR_EXIT_CODES
