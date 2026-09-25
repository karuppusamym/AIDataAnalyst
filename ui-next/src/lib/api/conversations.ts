/* ---------------------------------------------------------------------------
   R11-MP26: the caller's own Ask conversations (`aida.conversation_api`).

   A conversation is started and continued by the Ask routes themselves; these
   three only list, read and delete the caller's own. Someone else's answers
   404, exactly like one that does not exist. Questions come back as the server
   stored them -- identifying values replaced by `ATLAS_VALUE_<n>` tokens.
--------------------------------------------------------------------------- */

import type { ConversationRead, ConversationSummary } from "../types";
import { deleteRequest, demoOr, get } from "./transport";

/** `GET /v1/conversations?datasource_id=` -- the caller's own, most recent first. */
export function fetchConversations(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<ConversationSummary[]> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureConversations(datasourceId),
    async () =>
      get<ConversationSummary[]>(
        `/v1/conversations?datasource_id=${encodeURIComponent(datasourceId)}&limit=20`,
        signal,
      ),
  );
}

/** `GET /v1/conversations/{id}` -- one conversation with its turns. */
export function fetchConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureConversation(conversationId),
    async () =>
      get<ConversationRead>(`/v1/conversations/${encodeURIComponent(conversationId)}`, signal),
  );
}

/** `DELETE /v1/conversations/{id}` -- the thread goes; its runs stay (they are the audit
 *  record and never held the question text). */
export function deleteConversation(conversationId: string): Promise<void> {
  return demoOr(
    async (fixtures) => fixtures.deleteFixtureConversation(conversationId),
    async () => deleteRequest(`/v1/conversations/${encodeURIComponent(conversationId)}`),
  );
}
