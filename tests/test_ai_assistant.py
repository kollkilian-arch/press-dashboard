import hashlib
import unittest
from types import SimpleNamespace
from unittest import mock

import ai


def article(article_id, text):
    return {
        "id": article_id, "title": text, "full_text": text,
        "url": "https://example.com/article", "source_name": "Test",
        "published_at": "2026-09-16", "fetched_at": "2026-09-16",
        "category": "markt", "geschaeftsfeld": "Leben", "tags": "",
    }


class AssistantRetrievalTest(unittest.TestCase):
    def test_missing_and_stale_articles_are_searchable_without_reindexing(self):
        articles = [article(1, "Altersvorsorge Reform"), article(2, "Altersvorsorge Neuigkeiten")]
        metadata = [{"article_id": 1, "chunk_index": 0, "content_hash": "outdated"}]
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=articles),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=metadata),
            mock.patch.object(ai.db, "iter_article_chunks_for_pinned", return_value=iter([])) as chunks,
            mock.patch.object(ai, "refresh_pinned_article_chunks") as reindex,
            mock.patch.object(ai.db, "replace_article_chunks") as write,
            mock.patch.object(ai, "_embed_texts") as embed,
        ):
            contexts = ai.retrieve_pinned_article_context("Altersvorsorge")

        self.assertEqual({item["id"] for item in contexts}, {1, 2})
        self.assertIn("Neuigkeiten", contexts[1]["chunks"][0]["text"])
        chunks.assert_called_once_with([])
        reindex.assert_not_called()
        write.assert_not_called()
        embed.assert_not_called()

    def test_current_embeddings_are_reused_with_bounded_query_embedding(self):
        item = article(1, "Altersvorsorge Reform")
        content = ai._split_article_chunks(item)[0]
        metadata = [{
            "article_id": 1, "chunk_index": 0,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        }]
        row = {**item, "article_id": 1, "content": content,
               "embedding_model": "test-model", "embedding_json": "[1.0, 0.0]"}
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=[item]),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=metadata),
            mock.patch.object(ai.db, "iter_article_chunks_for_pinned", return_value=iter([row])) as chunks,
            mock.patch.object(ai, "_embed_texts", return_value=([[1.0, 0.0]], "test-model")) as embed,
        ):
            contexts = ai.retrieve_pinned_article_context("Altersvorsorge")

        chunks.assert_called_once_with([1])
        self.assertEqual(embed.call_args.kwargs, {"timeout": 5.0})
        self.assertEqual(contexts[0]["chunks"][0]["semantic_score"], 1.0)

    def test_query_embedding_timeout_falls_back_to_local_search(self):
        client = mock.Mock()
        client.with_options.return_value.embeddings.create.side_effect = TimeoutError()
        with (
            mock.patch.object(ai, "_get_embedding_api_key", return_value="test-key"),
            mock.patch.object(ai, "get_embedding_model", return_value="test-model"),
            mock.patch.object(ai, "_embedding_api_model", return_value="test-model"),
            mock.patch.object(ai, "_make_embedding_client", return_value=client),
            mock.patch.object(ai, "_EMBEDDING_LOCAL_FALLBACK_UNTIL", 0),
            mock.patch.dict(ai.os.environ, {"ASSISTANT_DISABLE_LOCAL_EMBEDDINGS": "0"}),
        ):
            vectors, model = ai._embed_texts(["Altersvorsorge"], timeout=5.0)

        client.with_options.assert_called_once_with(timeout=5.0, max_retries=0)
        client.close.assert_called_once()
        self.assertEqual(model, "local-hash-v1")
        self.assertEqual(len(vectors), 1)
        self.assertTrue(any(vectors[0]))

    def test_successful_embedding_closes_client_and_requests_float_encoding(self):
        client = mock.Mock()
        client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])])
        with (
            mock.patch.object(ai, "_get_embedding_api_key", return_value="test-key"),
            mock.patch.object(ai, "get_embedding_model", return_value="test-model"),
            mock.patch.object(ai, "_embedding_api_model", return_value="test-model"),
            mock.patch.object(ai, "_make_embedding_client", return_value=client),
            mock.patch.object(ai, "_EMBEDDING_LOCAL_FALLBACK_UNTIL", 0),
        ):
            vectors, model = ai._embed_texts(["Altersvorsorge"])
        self.assertEqual((vectors, model), ([[1.0, 0.0]], "test-model"))
        self.assertEqual(client.embeddings.create.call_args.kwargs["encoding_format"], "float")
        client.close.assert_called_once()

    def test_answer_client_closes_when_provider_raises(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = RuntimeError("provider unavailable")
        with (
            mock.patch.object(ai, "_rate_limit_cooldown_remaining", return_value=0),
            mock.patch.object(ai, "_extra_body_for_model", return_value={}),
            mock.patch.object(ai, "_make_client", return_value=client),
            self.assertRaisesRegex(RuntimeError, "provider unavailable"),
        ):
            ai._call("Question", model="test-model")
        client.close.assert_called_once()

    def test_reindex_still_builds_missing_embeddings(self):
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=[article(1, "Reform")]),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=[]),
            mock.patch.object(ai.db, "get_article_chunks_for_pinned") as vectors,
            mock.patch.object(ai, "_preferred_embedding_model_id", return_value="test-model"),
            mock.patch.object(ai, "_embed_texts", return_value=([[1.0]], "test-model")),
            mock.patch.object(ai.db, "replace_article_chunks") as write,
        ):
            result = ai.refresh_pinned_article_chunks()

        vectors.assert_not_called()
        write.assert_called_once()
        self.assertEqual(result["refreshed_articles"], 1)

    def test_reindex_stops_after_update_limit_and_reports_resume_cursor(self):
        articles = [article(i, "Altersvorsorge") for i in range(1, 9)]
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=articles),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=[]),
            mock.patch.object(ai, "_preferred_embedding_model_id", return_value="test-model"),
            mock.patch.object(ai, "_embed_texts", return_value=([[1.0]], "test-model")),
            mock.patch.object(ai.db, "replace_article_chunks") as write,
        ):
            result = ai.refresh_pinned_article_chunks(max_updates=2)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(result["last_article_id"], 2)
        self.assertTrue(result["has_more"])

    def test_failed_article_does_not_block_following_articles(self):
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=[article(1, "A"), article(2, "B")]),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=[]),
            mock.patch.object(ai, "_preferred_embedding_model_id", return_value="test-model"),
            mock.patch.object(ai, "_embed_texts", side_effect=[TimeoutError(), ([[1.0]], "test-model")]),
            mock.patch.object(ai.db, "replace_article_chunks") as write,
            self.assertLogs("ai"),
        ):
            result = ai.refresh_pinned_article_chunks()
        self.assertEqual(result["failed_articles"], 1)
        self.assertEqual(result["refreshed_articles"], 1)
        self.assertEqual(result["last_article_id"], 2)
        self.assertEqual(write.call_args.args[0], 2)

    def test_incomplete_embeddings_do_not_replace_existing_chunks(self):
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=[article(1, "A")]),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=[]),
            mock.patch.object(ai, "_preferred_embedding_model_id", return_value="test-model"),
            mock.patch.object(ai, "_embed_texts", return_value=([], "test-model")),
            mock.patch.object(ai.db, "replace_article_chunks") as write,
            self.assertLogs("ai"),
        ):
            result = ai.refresh_pinned_article_chunks()
        write.assert_not_called()
        self.assertEqual(result["failed_articles"], 1)

    def test_tag_order_does_not_invalidate_embeddings(self):
        item = article(1, "Reform")
        self.assertEqual(
            ai._split_article_chunks({**item, "tags": "rente,politik"}),
            ai._split_article_chunks({**item, "tags": "politik,rente"}),
        )

    def test_search_loads_vector_pages_lazily(self):
        first_page = [article(i, "Reform") for i in range(1, 41)]
        with (
            mock.patch.object(ai.db, "get_pinned_articles_for_assistant", return_value=first_page + [article(41, "Rente")]),
            mock.patch.object(ai.db, "get_article_chunk_metadata_for_pinned", return_value=[]),
            mock.patch.object(ai, "_assistant_search_page", side_effect=lambda articles, metadata: iter(articles)) as read,
        ):
            rows = ai._assistant_search_rows()
            self.assertEqual(next(rows)["id"], 1)
            self.assertEqual(read.call_count, 1)
            remaining = list(rows)
        self.assertEqual(remaining[-1]["id"], 41)
        self.assertEqual([len(call.args[0]) for call in read.call_args_list], [40, 1])


if __name__ == "__main__":
    unittest.main()
