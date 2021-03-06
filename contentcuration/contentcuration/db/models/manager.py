import contextlib
import logging as logger
import time
import uuid

from django.db import transaction
from django.db.models import Manager
from django.db.models import Q
from django.db.utils import OperationalError
from django_cte import CTEQuerySet
from le_utils.constants import content_kinds
from mptt.managers import TreeManager
from mptt.signals import node_moved

from contentcuration.db.models.query import CustomTreeQuerySet
from contentcuration.utils.tasks import increment_progress
from contentcuration.utils.tasks import set_total


logging = logger.getLogger(__name__)

# A default batch size of lft/rght values to process
# at once for copy operations
# Local testing has so far indicated that a batch size of 100
# gives much better overall copy performance than smaller batch sizes
# but does not hold locks on the affected MPTT tree for too long (~0.03s)
# Larger batch sizes seem to give slightly better copy performance
# but at the cost of much longer tree locking times.
# See test_duplicate_nodes_benchmark
# in contentcuration/contentcuration/tests/test_contentnodes.py
# for more details.
# The exact optimum batch size is probably highly dependent on tree
# topology also, so these rudimentary tests are likely insufficient
BATCH_SIZE = 100


class CustomManager(Manager.from_queryset(CTEQuerySet)):
    """
    The CTEManager improperly overrides `get_queryset`
    """

    pass


def log_lock_time_spent(timespent):
    logging.debug("Spent {} seconds inside an mptt lock".format(timespent))


def execute_queryset_without_results(queryset):
    query = queryset.query
    compiler = query.get_compiler(queryset.db)
    sql, params = compiler.as_sql()
    if not sql:
        return
    cursor = compiler.connection.cursor()
    cursor.execute(sql, params)


class CustomContentNodeTreeManager(TreeManager.from_queryset(CustomTreeQuerySet)):
    # Added 7-31-2018. We can remove this once we are certain we have eliminated all cases
    # where root nodes are getting prepended rather than appended to the tree list.
    def _create_tree_space(self, target_tree_id, num_trees=1):
        """
        Creates space for a new tree by incrementing all tree ids
        greater than ``target_tree_id``.
        """

        if target_tree_id == -1:
            raise Exception(
                "ERROR: Calling _create_tree_space with -1! Something is attempting to sort all MPTT trees root nodes!"
            )

        return super(CustomContentNodeTreeManager, self)._create_tree_space(
            target_tree_id, num_trees
        )

    def _get_next_tree_id(self, *args, **kwargs):
        from contentcuration.models import MPTTTreeIDManager

        new_id = MPTTTreeIDManager.objects.create().id
        return new_id

    @contextlib.contextmanager
    def _attempt_lock(self, tree_ids, values):
        """
        Internal method to allow the lock_mptt method to do retries in case of deadlocks
        """
        start = time.time()
        with transaction.atomic():
            # Issue a separate lock on each tree_id
            # in a predictable order.
            # This will mean that every process acquires locks in the same order
            # and should help to minimize deadlocks
            for tree_id in tree_ids:
                execute_queryset_without_results(
                    self.select_for_update()
                    .order_by()
                    .filter(tree_id=tree_id)
                    .values(*values)
                )
            yield
            log_lock_time_spent(time.time() - start)

    @contextlib.contextmanager
    def lock_mptt(self, *tree_ids):
        tree_ids = sorted((t for t in set(tree_ids) if t is not None))
        # If this is not inside the context of a delay context manager
        # or updates are not disabled set a lock on the tree_ids.
        if (
            not self.model._mptt_is_tracking
            and self.model._mptt_updates_enabled
            and tree_ids
        ):
            # Lock based on MPTT columns for updates on any of the tree_ids specified
            # until the end of this transaction
            mptt_opts = self.model._mptt_meta
            values = (
                mptt_opts.tree_id_attr,
                mptt_opts.left_attr,
                mptt_opts.right_attr,
                mptt_opts.level_attr,
                mptt_opts.parent_attr,
            )
            try:
                with self._attempt_lock(tree_ids, values):
                    yield
            except OperationalError as e:
                if "deadlock detected" in e.args[0]:
                    logging.error(
                        "Deadlock detected while trying to lock ContentNode trees for mptt operations, retrying"
                    )
                    with self._attempt_lock(tree_ids, values):
                        yield
                else:
                    raise
        else:
            # Otherwise just let it carry on!
            yield

    def partial_rebuild(self, tree_id):
        with self.lock_mptt(tree_id):
            return super(CustomContentNodeTreeManager, self).partial_rebuild(tree_id)

    def _move_child_to_new_tree(self, node, target, position):
        from contentcuration.models import PrerequisiteContentRelationship

        super(CustomContentNodeTreeManager, self)._move_child_to_new_tree(
            node, target, position
        )
        PrerequisiteContentRelationship.objects.filter(
            Q(prerequisite_id=node.id) | Q(target_node_id=node.id)
        ).delete()

    def _mptt_refresh(self, *nodes):
        """
        This is based off the MPTT model method mptt_refresh
        except that handles an arbitrary list of nodes to get
        the updated values in a single DB query.
        """
        ids = [node.id for node in nodes if node.id]
        # Don't bother doing a query if no nodes
        # were passed in
        if not ids:
            return
        opts = self.model._mptt_meta
        # Look up all the mptt field values
        # and the id so we can marry them up to the
        # passed in nodes.
        values_lookup = {
            # Create a lookup dict to cross reference
            # with the passed in nodes.
            c["id"]: c
            for c in self.filter(id__in=ids).values(
                "id",
                opts.left_attr,
                opts.right_attr,
                opts.level_attr,
                opts.tree_id_attr,
            )
        }
        for node in nodes:
            # Set the values on each of the nodes
            if node.id:
                values = values_lookup[node.id]
                for k, v in values.items():
                    setattr(node, k, v)

    def move_node(self, node, target, position="last-child"):
        """
        Vendored from mptt - by default mptt moves then saves
        This is updated to call the save with the skip_lock kwarg
        to prevent a second atomic transaction and tree locking context
        being opened.

        Moves ``node`` relative to a given ``target`` node as specified
        by ``position`` (when appropriate), by examining both nodes and
        calling the appropriate method to perform the move.
        A ``target`` of ``None`` indicates that ``node`` should be
        turned into a root node.
        Valid values for ``position`` are ``'first-child'``,
        ``'last-child'``, ``'left'`` or ``'right'``.
        ``node`` will be modified to reflect its new tree state in the
        database.
        This method explicitly checks for ``node`` being made a sibling
        of a root node, as this is a special case due to our use of tree
        ids to order root nodes.
        NOTE: This is a low-level method; it does NOT respect
        ``MPTTMeta.order_insertion_by``.  In most cases you should just
        move the node yourself by setting node.parent.
        """
        with self.lock_mptt(node.tree_id, target.tree_id):
            # Call _mptt_refresh to ensure that the mptt fields on
            # these nodes are up to date once we have acquired a lock
            # on the associated trees. This means that the mptt data
            # will remain fresh until the lock is released at the end
            # of the context manager.
            self._mptt_refresh(node, target)
            # N.B. this only calls save if we are running inside a
            # delay MPTT updates context
            self._move_node(node, target, position=position)
            node.save(skip_lock=True)
        node_moved.send(
            sender=node.__class__, instance=node, target=target, position=position,
        )

    def get_source_attributes(self, source):
        """
        These attributes will be copied when the node is copied
        and also when a copy is synced with its source
        """
        return {
            "content_id": source.content_id,
            "kind_id": source.kind_id,
            "title": source.title,
            "description": source.description,
            "language_id": source.language_id,
            "license_id": source.license_id,
            "license_description": source.license_description,
            "thumbnail_encoding": source.thumbnail_encoding,
            "extra_fields": source.extra_fields,
            "copyright_holder": source.copyright_holder,
            "author": source.author,
            "provider": source.provider,
            "role_visibility": source.role_visibility,
        }

    def _clone_node(
        self, source, parent_id, source_channel_id, can_edit_source_channel, pk, mods
    ):
        copy = {
            "id": pk or uuid.uuid4().hex,
            "node_id": uuid.uuid4().hex,
            "aggregator": source.aggregator,
            "cloned_source": source,
            "source_channel_id": source_channel_id,
            "source_node_id": source.node_id,
            "original_channel_id": source.original_channel_id,
            "original_source_node_id": source.original_source_node_id,
            "freeze_authoring_data": not can_edit_source_channel
            or source.freeze_authoring_data,
            "changed": True,
            "published": False,
            "parent_id": parent_id,
        }

        copy.update(self.get_source_attributes(source))

        if isinstance(mods, dict):
            copy.update(mods)

        # There might be some legacy nodes that don't have these, so ensure they are added
        if (
            copy["original_channel_id"] is None
            or copy["original_source_node_id"] is None
        ):
            original_node = source.get_original_node()
            if copy["original_channel_id"] is None:
                original_channel = original_node.get_channel()
                copy["original_channel_id"] = (
                    original_channel.id if original_channel else None
                )
            if copy["original_source_node_id"] is None:
                copy["original_source_node_id"] = original_node.node_id

        return copy

    def _recurse_to_create_tree(
        self,
        source,
        parent_id,
        source_channel_id,
        nodes_by_parent,
        source_copy_id_map,
        can_edit_source_channel,
        pk,
        mods,
    ):
        copy = self._clone_node(
            source, parent_id, source_channel_id, can_edit_source_channel, pk, mods,
        )

        if source.kind_id == content_kinds.TOPIC and source.id in nodes_by_parent:
            children = sorted(nodes_by_parent[source.id], key=lambda x: x.lft)
            copy["children"] = list(
                map(
                    lambda x: self._recurse_to_create_tree(
                        x,
                        copy["id"],
                        source_channel_id,
                        nodes_by_parent,
                        source_copy_id_map,
                        can_edit_source_channel,
                        None,
                        None,
                    ),
                    children,
                )
            )
        source_copy_id_map[source.id] = copy["id"]
        return copy

    def _all_nodes_to_copy(self, node, excluded_descendants):
        nodes_to_copy = node.get_descendants(include_self=True)

        if excluded_descendants:
            excluded_descendants = self.filter(
                node_id__in=excluded_descendants.keys()
            ).get_descendants(include_self=True)
            nodes_to_copy = nodes_to_copy.difference(excluded_descendants)
        return nodes_to_copy

    def copy_node(
        self,
        node,
        target=None,
        position="last-child",
        pk=None,
        mods=None,
        excluded_descendants=None,
        can_edit_source_channel=None,
        batch_size=None,
    ):
        if batch_size is None:
            batch_size = BATCH_SIZE
        source_channel_id = node.get_channel_id()

        total_nodes = self._all_nodes_to_copy(node, excluded_descendants).count()

        set_total(total_nodes)

        return self._copy(
            node,
            target,
            position,
            source_channel_id,
            pk,
            mods,
            excluded_descendants,
            can_edit_source_channel,
            batch_size,
        )

    def _copy(
        self,
        node,
        target,
        position,
        source_channel_id,
        pk,
        mods,
        excluded_descendants,
        can_edit_source_channel,
        batch_size,
    ):
        if node.rght - node.lft < batch_size:
            return self._deep_copy(
                node,
                target,
                position,
                source_channel_id,
                pk,
                mods,
                excluded_descendants,
                can_edit_source_channel,
            )
        else:
            node_copy = self._shallow_copy(
                node,
                target,
                position,
                source_channel_id,
                pk,
                mods,
                can_edit_source_channel,
            )
            children = node.get_children().order_by("lft")
            if excluded_descendants:
                children = children.exclude(node_id__in=excluded_descendants.keys())
            for child in children:
                self._copy(
                    child,
                    node_copy,
                    "last-child",
                    source_channel_id,
                    None,
                    None,
                    excluded_descendants,
                    can_edit_source_channel,
                    batch_size,
                )
            return [node_copy]

    def _parse_filter_kwargs(self, contentnode, contentnode__in):
        filter_kwargs = {}
        if contentnode is not None:
            filter_kwargs["contentnode"] = contentnode
        elif contentnode__in is not None:
            filter_kwargs["contentnode__in"] = contentnode__in
        else:
            raise ValueError("Must specify one of contentnode or contentnode__in")

        return filter_kwargs

    def _copy_tags(self, source_copy_id_map, contentnode, contentnode__in):
        from contentcuration.models import ContentTag

        filter_kwargs = self._parse_filter_kwargs(contentnode, contentnode__in)

        node_tags_mappings = list(
            self.model.tags.through.objects.filter(**filter_kwargs)
        )
        if contentnode is not None:
            tags_to_copy = ContentTag.objects.filter(
                tagged_content=contentnode, channel__isnull=False
            )
        elif contentnode__in is not None:
            tags_to_copy = ContentTag.objects.filter(
                tagged_content__in=contentnode__in, channel__isnull=False
            )

        # Get a lookup of all existing null channel tags so we don't duplicate
        existing_tags_lookup = {
            t["tag_name"]: t["id"]
            for t in ContentTag.objects.filter(
                tag_name__in=tags_to_copy.values_list("tag_name", flat=True),
                channel__isnull=True,
            ).values("tag_name", "id")
        }
        tags_to_copy = list(tags_to_copy)

        tags_to_create = []

        tag_id_map = {}

        for tag in tags_to_copy:
            if tag.tag_name in existing_tags_lookup:
                tag_id_map[tag.id] = existing_tags_lookup.get(tag.tag_name)
            else:
                new_tag = ContentTag(tag_name=tag.tag_name)
                tag_id_map[tag.id] = new_tag.id
                tags_to_create.append(new_tag)

        ContentTag.objects.bulk_create(tags_to_create)

        mappings_to_create = [
            self.model.tags.through(
                contenttag_id=tag_id_map.get(
                    mapping.contenttag_id, mapping.contenttag_id
                ),
                contentnode_id=source_copy_id_map.get(mapping.contentnode_id),
            )
            for mapping in node_tags_mappings
        ]

        self.model.tags.through.objects.bulk_create(mappings_to_create)

    def _copy_assessment_items(self, source_copy_id_map, contentnode, contentnode__in):
        from contentcuration.models import File
        from contentcuration.models import AssessmentItem

        filter_kwargs = self._parse_filter_kwargs(contentnode, contentnode__in)

        node_assessmentitems = list(AssessmentItem.objects.filter(**filter_kwargs))
        node_assessmentitem_files = list(
            File.objects.filter(assessment_item__in=node_assessmentitems)
        )

        assessmentitem_old_id_lookup = {}

        for assessmentitem in node_assessmentitems:
            old_id = assessmentitem.id
            assessmentitem.id = None
            assessmentitem.contentnode_id = source_copy_id_map[
                assessmentitem.contentnode_id
            ]
            assessmentitem_old_id_lookup[
                assessmentitem.contentnode_id + ":" + assessmentitem.assessment_id
            ] = old_id

        node_assessmentitems = AssessmentItem.objects.bulk_create(node_assessmentitems)

        assessmentitem_new_id_lookup = {}

        for assessmentitem in node_assessmentitems:
            old_id = assessmentitem_old_id_lookup[
                assessmentitem.contentnode_id + ":" + assessmentitem.assessment_id
            ]
            assessmentitem_new_id_lookup[old_id] = assessmentitem.id

        for file in node_assessmentitem_files:
            file.id = None
            file.assessment_item_id = assessmentitem_new_id_lookup[
                file.assessment_item_id
            ]

        File.objects.bulk_create(node_assessmentitem_files)

    def _copy_files(self, source_copy_id_map, contentnode, contentnode__in):
        from contentcuration.models import File

        filter_kwargs = self._parse_filter_kwargs(contentnode, contentnode__in)

        node_files = list(File.objects.filter(**filter_kwargs))

        for file in node_files:
            file.id = None
            file.contentnode_id = source_copy_id_map[file.contentnode_id]

        File.objects.bulk_create(node_files)

    def _copy_associated_objects(
        self, source_copy_id_map, contentnode=None, contentnode__in=None
    ):
        self._copy_files(source_copy_id_map, contentnode, contentnode__in)

        self._copy_assessment_items(source_copy_id_map, contentnode, contentnode__in)

        self._copy_tags(source_copy_id_map, contentnode, contentnode__in)

    def _shallow_copy(
        self,
        node,
        target,
        position,
        source_channel_id,
        pk,
        mods,
        can_edit_source_channel,
    ):
        data = self._clone_node(
            node, None, source_channel_id, can_edit_source_channel, pk, mods,
        )
        with self.lock_mptt(target.tree_id if target else None):
            node_copy = self.model(**data)
            if target:
                self._mptt_refresh(target)
            self.insert_node(node_copy, target, position=position, save=False)
            node_copy.save(force_insert=True)

        self._copy_associated_objects(
            {node.id: node_copy.id}, contentnode=node,
        )
        increment_progress(1)
        return node_copy

    def _deep_copy(
        self,
        node,
        target,
        position,
        source_channel_id,
        pk,
        mods,
        excluded_descendants,
        can_edit_source_channel,
    ):

        nodes_to_copy = self._all_nodes_to_copy(node, excluded_descendants)

        nodes_by_parent = {}

        for copy_node in nodes_to_copy:
            if copy_node.parent_id not in nodes_by_parent:
                nodes_by_parent[copy_node.parent_id] = []
            nodes_by_parent[copy_node.parent_id].append(copy_node)

        source_copy_id_map = {}

        data = self._recurse_to_create_tree(
            node,
            target.id if target else None,
            source_channel_id,
            nodes_by_parent,
            source_copy_id_map,
            can_edit_source_channel,
            pk,
            mods,
        )

        with self.lock_mptt(target.tree_id if target else None):
            if target:
                self._mptt_refresh(target)
            nodes_to_create = self.build_tree_nodes(
                data, target=target, position=position
            )
            new_nodes = self.bulk_create(nodes_to_create)
        if target:
            self.filter(pk=target.pk).update(changed=True)

        self._copy_associated_objects(source_copy_id_map, contentnode__in=nodes_to_copy)

        increment_progress(len(nodes_to_copy))

        return new_nodes

    def build_tree_nodes(self, data, target=None, position="last-child"):
        """
        vendored from:
        https://github.com/django-mptt/django-mptt/blob/fe2b9cc8cfd8f4b764d294747dba2758147712eb/mptt/managers.py#L614
        """
        opts = self.model._mptt_meta
        if target:
            tree_id = target.tree_id
            if position in ("left", "right"):
                level = getattr(target, opts.level_attr)
                if position == "left":
                    cursor = getattr(target, opts.left_attr)
                else:
                    cursor = getattr(target, opts.right_attr) + 1
            else:
                level = getattr(target, opts.level_attr) + 1
                if position == "first-child":
                    cursor = getattr(target, opts.left_attr) + 1
                else:
                    cursor = getattr(target, opts.right_attr)
        else:
            tree_id = self._get_next_tree_id()
            cursor = 1
            level = 0

        stack = []

        def treeify(data, cursor=1, level=0):
            data = dict(data)
            children = data.pop("children", [])
            node = self.model(**data)
            stack.append(node)
            setattr(node, opts.tree_id_attr, tree_id)
            setattr(node, opts.level_attr, level)
            setattr(node, opts.left_attr, cursor)
            for child in children:
                cursor = treeify(child, cursor=cursor + 1, level=level + 1)
            cursor += 1
            setattr(node, opts.right_attr, cursor)
            return cursor

        treeify(data, cursor=cursor, level=level)

        if target:
            self._create_space(2 * len(stack), cursor - 1, tree_id)

        return stack
