"""Multi Modal Vector Store Index.

An index that that is built on top of multiple vector stores for different modalities.

"""
import logging
from typing import Any, List, Optional, Sequence

from llama_index.data_structs.data_structs import IndexDict, MultiModelIndexDict
from llama_index.embeddings.mutli_modal_base import MultiModalEmbedding
from llama_index.embeddings.utils import EmbedType, resolve_embed_model
from llama_index.indices.base_retriever import BaseRetriever
from llama_index.indices.service_context import ServiceContext
from llama_index.indices.utils import (
    async_embed_image_nodes,
    async_embed_nodes,
    embed_image_nodes,
    embed_nodes,
)
from llama_index.indices.vector_store.base import VectorStoreIndex
from llama_index.schema import BaseNode, ImageNode
from llama_index.storage.storage_context import StorageContext
from llama_index.vector_stores.simple import DEFAULT_VECTOR_STORE, SimpleVectorStore
from llama_index.vector_stores.types import VectorStore

logger = logging.getLogger(__name__)


class MultiModalVectorStoreIndex(VectorStoreIndex):
    """Multi-Modal Vector Store Index.

    Args:
        use_async (bool): Whether to use asynchronous calls. Defaults to False.
        show_progress (bool): Whether to show tqdm progress bars. Defaults to False.
        store_nodes_override (bool): set to True to always store Node objects in index
            store and document store even if vector store keeps text. Defaults to False
    """

    image_namespace = "image"
    index_struct_cls = MultiModelIndexDict

    def __init__(
        self,
        nodes: Optional[Sequence[BaseNode]] = None,
        index_struct: Optional[MultiModelIndexDict] = None,
        service_context: Optional[ServiceContext] = None,
        storage_context: Optional[StorageContext] = None,
        use_async: bool = False,
        store_nodes_override: bool = False,
        show_progress: bool = False,
        # Image-related kwargs
        image_vector_store: Optional[VectorStore] = None,
        image_embed_model: EmbedType = "clip",
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        image_embed_model = resolve_embed_model(image_embed_model)
        assert isinstance(image_embed_model, MultiModalEmbedding)
        self._image_embed_model = image_embed_model

        storage_context = storage_context or StorageContext.from_defaults()

        if image_vector_store is not None:
            storage_context.add_vector_store(image_vector_store, self.image_namespace)

        if self.image_namespace not in storage_context.vector_stores:
            storage_context.add_vector_store(SimpleVectorStore(), self.image_namespace)

        self._image_vector_store = storage_context.vector_stores[self.image_namespace]

        super().__init__(
            nodes=nodes,
            index_struct=index_struct,
            service_context=service_context,
            storage_context=storage_context,
            show_progress=show_progress,
            use_async=use_async,
            store_nodes_override=store_nodes_override,
            **kwargs,
        )

    @property
    def image_vector_store(self) -> VectorStore:
        return self._image_vector_store

    @property
    def image_embed_model(self) -> EmbedType:
        return self._image_embed_model

    def as_retriever(self, **kwargs: Any) -> BaseRetriever:
        # NOTE: lazy import
        from llama_index.indices.multi_modal.retriever import (
            MultiModalVectorIndexRetriever,
        )

        return MultiModalVectorIndexRetriever(
            self,
            node_ids=list(self.index_struct.nodes_dict.values()),
            **kwargs,
        )

    @classmethod
    def from_vector_store(
        cls,
        vector_store: VectorStore,
        service_context: Optional[ServiceContext] = None,
        # Image-related kwargs
        image_vector_store: Optional[VectorStore] = None,
        image_embed_model: EmbedType = "clip",
        **kwargs: Any,
    ) -> "VectorStoreIndex":
        if not vector_store.stores_text:
            raise ValueError(
                "Cannot initialize from a vector store that does not store text."
            )

        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        return cls(
            nodes=[],
            service_context=service_context,
            storage_context=storage_context,
            image_vector_store=image_vector_store,
            image_embed_model=image_embed_model,
            **kwargs,
        )

    def _get_node_with_embedding(
        self,
        nodes: Sequence[BaseNode],
        show_progress: bool = False,
        is_image: bool = False,
    ) -> List[BaseNode]:
        """Get tuples of id, node, and embedding.

        Allows us to store these nodes in a vector store.
        Embeddings are called in batches.

        """
        if is_image:
            id_to_embed_map = embed_image_nodes(
                nodes,
                embed_model=self._image_embed_model,
                show_progress=show_progress,
            )
        else:
            id_to_embed_map = embed_nodes(
                nodes,
                embed_model=self._service_context.embed_model,
                show_progress=show_progress,
            )

        results = []
        for node in nodes:
            embedding = id_to_embed_map[node.node_id]
            result = node.copy()
            result.embedding = embedding
            results.append(result)
        return results

    async def _aget_node_with_embedding(
        self,
        nodes: Sequence[BaseNode],
        show_progress: bool = False,
        is_image: bool = False,
    ) -> List[BaseNode]:
        """Asynchronously get tuples of id, node, and embedding.

        Allows us to store these nodes in a vector store.
        Embeddings are called in batches.

        """
        if is_image:
            id_to_embed_map = await async_embed_image_nodes(
                nodes,
                embed_model=self._image_embed_model,
                show_progress=show_progress,
            )
        else:
            id_to_embed_map = await async_embed_nodes(
                nodes,
                embed_model=self._service_context.embed_model,
                show_progress=show_progress,
            )

        results = []
        for node in nodes:
            embedding = id_to_embed_map[node.node_id]
            result = node.copy()
            result.embedding = embedding
            results.append(result)
        return results

    async def _async_add_nodes_to_index(
        self,
        index_struct: IndexDict,
        nodes: Sequence[BaseNode],
        show_progress: bool = False,
        **insert_kwargs: Any,
    ) -> None:
        """Asynchronously add nodes to index."""
        if not nodes:
            return

        image_nodes: List[ImageNode] = []
        text_nodes: List[BaseNode] = []

        for node in nodes:
            if isinstance(node, ImageNode):
                image_nodes.append(node)
            if node.text:
                text_nodes.append(node)

        # embed all nodes as text - incclude image nodes that have text attached
        text_nodes = await self._aget_node_with_embedding(
            text_nodes, show_progress, is_image=False
        )
        new_text_ids = await self.storage_context.vector_stores[
            DEFAULT_VECTOR_STORE
        ].async_add(text_nodes, **insert_kwargs)

        # embed image nodes as images directly
        image_nodes = await self._aget_node_with_embedding(
            image_nodes, show_progress, is_image=True
        )
        new_img_ids = await self.storage_context.vector_stores[
            self.image_namespace
        ].async_add(image_nodes, **insert_kwargs)

        # if the vector store doesn't store text, we need to add the nodes to the
        # index struct and document store
        all_nodes = text_nodes + image_nodes
        all_new_ids = new_text_ids + new_img_ids
        if not self._vector_store.stores_text or self._store_nodes_override:
            for node, new_id in zip(all_nodes, all_new_ids):
                # NOTE: remove embedding from node to avoid duplication
                node_without_embedding = node.copy()
                node_without_embedding.embedding = None

                index_struct.add_node(node_without_embedding, text_id=new_id)
                self._docstore.add_documents(
                    [node_without_embedding], allow_update=True
                )

    def _add_nodes_to_index(
        self,
        index_struct: IndexDict,
        nodes: Sequence[BaseNode],
        show_progress: bool = False,
        **insert_kwargs: Any,
    ) -> None:
        """Add document to index."""
        if not nodes:
            return

        image_nodes: List[ImageNode] = []
        text_nodes: List[BaseNode] = []

        for node in nodes:
            if isinstance(node, ImageNode):
                image_nodes.append(node)
            if node.text:
                text_nodes.append(node)

        # embed all nodes as text - incclude image nodes that have text attached
        text_nodes = self._get_node_with_embedding(
            text_nodes, show_progress, is_image=False
        )
        new_text_ids = self.storage_context.vector_stores[DEFAULT_VECTOR_STORE].add(
            text_nodes, **insert_kwargs
        )

        # embed image nodes as images directly
        image_nodes = self._get_node_with_embedding(
            image_nodes, show_progress, is_image=True
        )
        new_img_ids = self.storage_context.vector_stores[self.image_namespace].add(
            image_nodes, **insert_kwargs
        )

        # if the vector store doesn't store text, we need to add the nodes to the
        # index struct and document store
        all_nodes = text_nodes + image_nodes
        all_new_ids = new_text_ids + new_img_ids
        if not self._vector_store.stores_text or self._store_nodes_override:
            for node, new_id in zip(all_nodes, all_new_ids):
                # NOTE: remove embedding from node to avoid duplication
                node_without_embedding = node.copy()
                node_without_embedding.embedding = None

                index_struct.add_node(node_without_embedding, text_id=new_id)
                self._docstore.add_documents(
                    [node_without_embedding], allow_update=True
                )

    def delete_ref_doc(
        self, ref_doc_id: str, delete_from_docstore: bool = False, **delete_kwargs: Any
    ) -> None:
        """Delete a document and it's nodes by using ref_doc_id."""
        # delete from all vector stores

        for vector_store in self._storage_context.vector_stores.values():
            vector_store.delete(ref_doc_id)

            if self._store_nodes_override or self._vector_store.stores_text:
                ref_doc_info = self._docstore.get_ref_doc_info(ref_doc_id)
                if ref_doc_info is not None:
                    for node_id in ref_doc_info.node_ids:
                        self._index_struct.delete(node_id)
                        self._vector_store.delete(node_id)

        if delete_from_docstore:
            self._docstore.delete_ref_doc(ref_doc_id, raise_error=False)

        self._storage_context.index_store.add_index_struct(self._index_struct)
