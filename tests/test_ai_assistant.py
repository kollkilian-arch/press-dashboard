import hashlib
import unittest
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
            mock.patch.object(ai.db, "get_article_chunks_for_pinned", return_value=[]) as chunks,
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
            mock.patch.object(ai.db, "get_article_chunks_for_pinned", return_value=[row]) as chunks,
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
        self.assertEqual(model, "local-hash-v1")
        self.assertEqual(len(vectors), 1)
        self.assertTrue(any(vectors[0]))

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


if __name__ == "__main__":
    unittest.main()
