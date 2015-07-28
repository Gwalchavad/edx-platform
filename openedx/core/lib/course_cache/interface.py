"""
...
"""
from collections import defaultdict, namedtuple

from opaque_keys.edx.keys import UsageKey
from xmodule.modulestore.django import modulestore

from .graph_traversals import traverse_topologically


def _filter_block_data_dict(block_data_dict, block_structure):
    """
    Arguments:
        block_data_dict (dict[UsageKey: CourseBlockData])
        block_structure: CourseBlockStructure

    Returns:
        dict[UsageKey: CourseBlockData]
    """
    keys_in_structure = block_structure.get_block_keys()
    return {
        usage_key: block_data
        for usage_key, block_data in block_data_dict.iteritems()
        if usage_key in keys_in_structure
    }


class CourseCacheInterface(object):
    """
    ...
    """
    # TODO me: Make sure cache keys will not cause conflicts.

    def __init__(self, cache, course_cache_key_prefix, block_cache_key_prefix, available_transformations):
        """
        Arguments:
            cache (BaseCache)
            course_cache_key_prefix (str)
            block_cache_key_prefix (str)
            available_transformations (list[Transformation])
        """
        self._cache = cache
        self._course_cache_key_prefix = course_cache_key_prefix
        self._block_cache_key_prefix = block_cache_key_prefix
        self._len_block_cache_key_prefix = len(self._block_cache_key_prefix)
        self._available_transformations = available_transformations

    def get_course_blocks(self, user, course_key, transformations, root_block_key=None):
        """
        Arguments:
            user (User)
            course_key (CourseKey): Course to which desired blocks belong.
            transformations (list[Transformation])
            root_block_key (UsageKey): Usage key for root block in the subtree
                for which block information will be returned. Passing in the usage
                key of a course will return the entire user-specific course
                hierarchy.

        Returns:
            (CourseBlockStructure, dict[UsageKey: CourseBlockData])
        """

        # Load the cached course structure.
        full_block_structure = self._get_cached_block_structure(course_key)

        # If the structure is in the cache, then extract the requested sub-structure
        # and load the necessary block data.
        if full_block_structure:
            block_structure = (
                full_block_structure.get_sub_structure(root_block_key)
                if root_block_key
                else full_block_structure
            )
            block_data_dict = self._get_cached_block_data_dict(
                course_key,
                block_structure.get_block_keys()
            )

        # Else:
        # (1) Load the entire course and extract its structure.
        # (2) Load block data for the entire course structure.
        # (3) Cache this information.
        # (4) Extract the requested sub-structure.
        # (5) Load the necessary block data.
        else:
            full_block_structure, xblock_dict = self._create_block_structure(course_key)
            block_data_dict = self._create_block_data_dict(full_block_structure, xblock_dict)
            self._cache_block_structure(course_key, full_block_structure)
            self._cache_block_data_dict(block_data_dict)
            block_structure = (
                full_block_structure.get_sub_structure(root_block_key)
                if root_block_key
                else full_block_structure
            )

        # Apply transformations to course structure and data.
        for transformation in transformations:
            transformation.apply(user, course_key, block_structure, block_data_dict)

        # Filter out blocks that were removed during transformation application.
        return block_structure, _filter_block_data_dict(block_data_dict, block_structure)

    def _encode_course_key(self, course_key):
        """
        Arguments:
            course_key (CourseKey)

        Returns:
            str
        """
        return self._course_cache_key_prefix + str(course_key)

    def _encode_usage_key(self, usage_key):
        """
        Arguments:
            usage_key (UsageKey)

        Returns:
            str
        """
        return self._block_cache_key_prefix + str(usage_key)

    def _decode_usage_key(self, course_key, cache_key):
        """
        Arguments:
            course_key (CourseKey): The course to which the returned usage_key's
                block belongs.
            cache_key (str)

        Returns:
            usage_key (UsageKey)
        """
        return UsageKey.from_string(
            cache_key[self._len_block_cache_key_prefix:]
        ).map_into_course(course_key)

    @staticmethod
    def _create_block_structure(course_key):
        """
        Arguments:
            course_key (CourseKey)

        Returns:
            (CourseBlockStructure, dict[UsageKey: XBlock])
        """
        visited_keys = set()
        xblock_dict = {}
        adj = defaultdict(lambda: CourseBlockStructure.AdjacencyInfo(set(), set()))

        def build_block_structure(xblock):
            """
            Helper function to recursively walk course structure and build
            xblock_dict and adj.

            Arguments:
                xblock (XBlock)
            """
            visited_keys.add(xblock.location)
            xblock_dict[xblock.location] = xblock

            for child in xblock.get_children():
                adj[xblock.location].children.add(child.location)
                adj[child.location].parents.add(xblock.location)
                if child.location not in visited_keys:
                    build_block_structure(child)

        course = modulestore().get_course(course_key, depth=None)  # depth=None => load entire course
        build_block_structure(course)
        block_structure = CourseBlockStructure(course.location, True, adj)
        return block_structure, xblock_dict

    def _create_block_data_dict(self, block_structure, xblock_dict):
        """
        Arguments:
            block_structure (CourseBlockStructure)
            xblock_dict (dict[UsageKey: XBlock])
            transformations (list[Transformation])

        Returns:
            dict[UsageKey: CourseBlockData]
        """
        if not block_structure.root_block_is_course_root:
            raise ValueError("block_structure must be entire course hierarchy.")

        # Define functions for traversing course hierarchy.
        get_children = lambda xblock: [
            xblock_dict[child_key]
            for child_key in block_structure.get_children(xblock.location)
        ]
        get_parents = lambda xblock: [
            xblock_dict[child_key]
            for child_key in block_structure.get_parents(xblock.location)
        ]

        # For each transformation, extract required fields and collect specially
        # computed data.
        required_fields = set()
        collected_data = {}
        for transformation in self._available_transformations:
            required_fields |= transformation.required_fields
            collected_data[transformation.id] = transformation.collect(
                xblock_dict[block_structure.root_block_key],
                get_children,
                get_parents
            )

        # Build a dictionary mapping usage keys to block information.
        return {
            usage_key: CourseBlockData(
                {
                    required_field.name: getattr(xblock, required_field.name, None)
                    for required_field in required_fields
                },
                {
                    transformation_id: transformation_data.get(usage_key, None)
                    for transformation_id, transformation_data in collected_data.iteritems()
                }
            )
            for usage_key, xblock in xblock_dict.iteritems()
        }

    def clear_course(self, course_key):
        """
        Arguments:
            course_key (CourseKey)

        It is safe to call this with a course_key that isn't in the cache.
        """
        self._cache.delete(self._encode_course_key(course_key))

    def _cache_block_structure(self, course_key, block_structure):
        """
        Arguments:
            block_structure (CourseBlockStructure)
        """
        if not block_structure.root_block_is_course_root:
            raise ValueError("block_structure must be entire course hierarchy.")
        child_map = {
            usage_key: block_structure.get_children(usage_key)
            for usage_key in block_structure.get_block_keys()
        }
        self._cache.set(
            self._encode_course_key(course_key),
            (block_structure.root_block_key, child_map)
        )

    def _cache_block_data_dict(self, block_data_dict):
        """
        Arguments:
            block_data_dict (dict[UsageKey: CourseBlockData])
        """
        self._cache.set_many({
            self._encode_usage_key(usage_key): block_data
            for usage_key, block_data
            in block_data_dict.iteritems()
        })

    def _get_cached_block_structure(self, course_key):
        """
        Arguments:
            course_key (CourseKey)
            root_block_key (UsageKey or NoneType)

        Returns:
            CourseBlockStructure, if the block structure is in the cache, and
            NoneType otherwise.
        """
        cached = self._cache.get(self._encode_course_key(course_key), None)
        if not cached:
            return None
        course_root_block_key, child_map = cached

        # We have a singly-linked DAG (child_map).
        # We want to create a doubly-linked DAG.
        # To do so, we must populate a parent map.

        # For each block...
        parent_map = defaultdict(set)
        for usage_key, children in child_map.iteritems():
            # For each child of the block...
            for child in children:
                # Add the block to the child's set of parents.
                parent_map[child].add(usage_key)

        # Zip parent_map and child_map together to construct an adjacency list.
        adj = {
            usage_key: CourseBlockStructure.AdjacencyInfo(parent_map[usage_key], children)
            for usage_key, children in child_map.iteritems()
        }
        return CourseBlockStructure(course_root_block_key, True, adj)

    def _get_cached_block_data_dict(self, course_key, usage_keys):
        """
        Arguments:
            course_key (CourseKey)
            usage_keys (list[UsageKey])

        Returns:
            dict[UsageKey: CourseBlockData]
        """
        block_cache_keys = [self._encode_usage_key(usage_key) for usage_key in usage_keys]
        return {
            self._decode_usage_key(course_key, block_cache_key): block_data
            for block_cache_key, block_data
            in self._cache.get_many(block_cache_keys).iteritems()
        }


class CourseBlockStructure(object):
    """
    A wrapper around a doubly-linked directed acyclic graph of XBlock UsageKeys.
    """

    # parents and children are of type set[UsageKey].
    AdjacencyInfo = namedtuple('AdjacencyInfo', 'parents children')

    def __init__(self, root_block_key, root_block_is_course_root, adj):
        """
        Arguments:
            adj (dict[UsageKey: AdjacencyInfo])
        """
        self.root_block_key = root_block_key
        self.root_block_is_course_root = root_block_is_course_root
        self._adj = adj

    def __str__(self):
        """
        Returns:
            str
        """
        # TODO me: This version is just for debugging; rewrite or remove this.
        return '{{"root_block_key": "{}", "root_block_is_course_root": "{}", "adj": {{{}}}}}'.format(
            self.root_block_key, self.root_block_is_course_root,
            ", ".join([
                '"{}": {{"parents": [{}], "children": [{}]}}'.format(
                    u,
                    ", ".join(['"{}"'.format(p) for p in parents]),
                    ", ".join(['"{}"'.format(c) for c in children]),
                )
                for u, (parents, children) in self._adj.iteritems()
            ])
        )

    def get_block_keys(self):
        """
        Returns:
            set[usage_key]
        """
        return set(self._adj.keys())

    def get_parents(self, usage_key):
        """
        Arguments:
            usage_key (UsageKey)

        Returns:
            set[UsageKey]
        """
        return self._adj[usage_key].parents

    def get_children(self, usage_key):
        """
        Arguments:
            usage_key (UsageKey)

        Returns:
            set[UsageKey]
        """
        return self._adj[usage_key].children

    def get_sub_structure(self, root_block_key):
        """
        Arguments:
            root_block_key (UsageKey)

        Returns:
            CourseBlockStructure
        """
        if root_block_key == self.root_block_key:
            return self
        nodes = set(traverse_topologically(
            start_node=self.root_block_key,
            get_parents=self.get_parents,
            get_children=self.get_children
        ))
        adj = {
            usage_key: (
                children,
                set(parent for parent in self.get_parents(usage_key) if parent in nodes)
            )
            for usage_key, (parents, children) in self._adj.iteritems()
            if usage_key in nodes
        }
        return CourseBlockStructure(root_block_key, False, adj)

    def _remove_block(self, usage_key, remove_orphans):
        """
        Arguments:
            usage_key (UsageKey)
            remove_orphans (bool): If True, recursively remove all blocks that
                become orphans as a result of this block removal, along with
                all blocks that subsequently become orphans, and so on. Note
                that this incurs a significant performance hit.

        Raises:
            KeyError if block does not exist in course structure.
        """
        adj = self._adj

        # For all this block's children, remove self from list of their parents.
        for child in adj[usage_key].children:
            adj[child].parents.remove(usage_key)

            # If this is the child's only parent, then the child is now an
            # orphan. If requested, recursively remove it as well.
            if remove_orphans and not adj[child].parents:
                self._remove_block(child, remove_orphans)

        # For all this block's parents, remove self from list of their children.
        for parent_key in adj[usage_key].parents:
            adj[parent_key].children.remove(usage_key)

        # Remove adjacency list entry
        del adj[usage_key]

    def remove_block(self, usage_key):
        """
        Arguments:
            usage_key (UsageKey)

        Raises:
            KeyError if block does not exist in course structure.
        """
        # This is just a wrapper around _remove_block because we don't want to
        # expose the remove_orphans option.
        self._remove_block(usage_key, remove_orphans=True)

    def remove_block_if(self, removal_condition):
        """
        Arguments:
            removal_condition (UsageKey -> bool)
        """
        # Trivial case: If the root block satisfies the removal condition, then
        # just remove the entire structure.
        if removal_condition(self.root_block_key):
            self._adj = []
            return

        traversal = traverse_topologically(
            start_node=self.root_block_key,
            get_parents=self.get_parents,
            get_children=self.get_children
        )

        # Skip the first (root) block because
        # (a) We already checked it against the removal condition, and
        # (b) It would be wrongly flagged as is_orphan in the next for loop.
        __ = next(traversal)

        for usage_key in traversal:
            is_orphan = not self._adj[usage_key].parents
            if is_orphan or removal_condition(usage_key):
                # Because we're doing a topological sort, removing blocks can
                # only create orphans *later* in the traversal. So, we save time
                # by passing remove_orphans=False and handling orphan removal
                # ourselves.
                self._remove_block(usage_key, remove_orphans=False)


class CourseBlockData(object):
    """
    ...
    """

    def __init__(self, block_fields, transformation_data):
        """
        Arguments:
            block_fields (dict[str: *])
            transformation_data (dict[str: dict]):
                Dictionary mapping transformations' IDs to their collected data.
                {
                    'builtin.visibility': { 'visible_to_staff_only': ... }
                    'another_trans_id': { 'key1': value, 'key2': value2 ... }
                    ...
                }
        """
        self._block_fields = block_fields
        self._transformation_data = transformation_data

    def __str__(self):
        """
        Returns:
            str
        """
        # TODO me: This version is just for debugging; rewrite or remove this.
        return '{{"block_fields": {{{}}}, {}}}'.format(
            ", ".join([
                '"{}": "{}"'.format(str(key), value)
                for key, value in self._block_fields.iteritems()
            ]),
            ", ".join([
                '"{}": {{{}}}'.format(key, (
                    ", ".join([
                        '"{}": "{}"'.format(k, v)
                        for k, v in value.iteritems()
                    ])
                ))
                for key, value in self._transformation_data.iteritems()
            ])
        )

    def get_block_field(self, field_name):
        """
        Arguments:
            field_name: str

        Returns:
            *
        """
        return self._block_fields[field_name]

    def get_transformation_data(self, transformation, key):
        """
        Arguments:
            transformation_id: str
            key: str

        Returns:
            *
        """
        if transformation.id in self._transformation_data:
            if key in self._transformation_data[transformation.id]:
                return self._transformation_data[transformation.id][key]
            else:
                return None
        else:
            raise KeyError(
                "Data for transformation with ID {} not found.".format(
                    transformation.id
                )
            )
