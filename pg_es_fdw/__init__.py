""" Elastic Search foreign data wrapper """
# pylint: disable=too-many-instance-attributes, import-error, unexpected-keyword-arg, broad-except, line-too-long

import json
import logging

from elasticsearch import VERSION as ELASTICSEARCH_VERSION
from elasticsearch import Elasticsearch
from multicorn import ForeignDataWrapper
from multicorn.utils import log_to_postgres as log2pg


class ElasticsearchFDW(ForeignDataWrapper):
    """ Elastic Search Foreign Data Wrapper """

    @property
    def rowid_column(self):
        """ Returns a column name which will act as a rowid column for
            delete/update operations.

            This can be either an existing column name, or a made-up one. This
            column name should be subsequently present in every returned
            resultset. """

        return self._rowid_column

    def __init__(self, options, columns):
        super(ElasticsearchFDW, self).__init__(options, columns)

        self.index = options.pop("index", "")
        self.doc_type = options.pop("type", "")
        self.query_column = options.pop("query_column", None)
        self.score_column = options.pop("score_column", None)
        self.default_sort = options.pop("default_sort", "")
        self.sort_column = options.pop("sort_column", None)
        self.scroll_size = int(options.pop("scroll_size", "1000"))
        self.scroll_duration = options.pop("scroll_duration", "10m")
        self._rowid_column = options.pop("rowid_column", "id")
        username = options.pop("username", None)
        password = options.pop("password", None)

        if ELASTICSEARCH_VERSION[0] >= 7:
            self.path = "/{index}".format(index=self.index)
            self.arguments = {"index": self.index}
        else:
            self.path = "/{index}/{doc_type}".format(
                index=self.index, doc_type=self.doc_type
            )
            self.arguments = {"index": self.index, "doc_type": self.doc_type}

        if (username is None) != (password is None):
            raise ValueError("Must provide both username and password")
        if username is not None:
            auth = (username, password)
        else:
            auth = None

        host = options.pop("host", "localhost")
        port = int(options.pop("port", "9200"))
        timeout = int(options.pop("timeout", "10"))
        self.client = Elasticsearch(
            [{"host": host, "port": port}], http_auth=auth, timeout=timeout, **options
        )

        self.columns = columns
        self.json_columns = {
            column.column_name
            for column in columns.values()
            if column.base_type_name.upper() in {"JSON", "JSONB"}
        }

    def get_rel_size(self, quals, columns):
        """ Helps the planner by returning costs.
            Returns a tuple of the form (number of rows, average row width) """

        try:
            query = self._get_query(quals)
            if query:
                response = self.client.count(q=query, **self.arguments)
            else:
                response = self.client.count(**self.arguments)
            return (response["count"], len(columns) * 100)
        except Exception as exception:
            log2pg(
                "COUNT for {path} failed: {exception}".format(
                    path=self.path, exception=exception
                ),
                logging.ERROR,
            )
            return (0, 0)

    def execute(self, quals, columns):
        """ Execute the query """

        try:
            arguments = dict(self.arguments)
            arguments['sort'] = self._get_sort(quals)
            sort = arguments['sort']
            query = self._get_query(quals)

            if query:
                response = self.client.search(
                    size=self.scroll_size,
                    scroll=self.scroll_duration,
                    q=query,
                    **arguments
                )
            else:
                response = self.client.search(
                    size=self.scroll_size, scroll=self.scroll_duration, **arguments
                )

            while True:
                scroll_id = response["_scroll_id"]

                for result in response["hits"]["hits"]:
                    yield self._convert_response_row(result, columns, query, sort)

                if len(response["hits"]["hits"]) < self.scroll_size:
                    return
                response = self.client.scroll(
                    scroll_id=scroll_id, scroll=self.scroll_duration
                )
        except Exception as exception:
            log2pg(
                "SEARCH for {path} failed: {exception}".format(
                    path=self.path, exception=exception
                ),
                logging.ERROR,
            )
            return

    def insert(self, new_values):
        """ Insert new documents into Elastic Search """

        if self.rowid_column not in new_values:
            log2pg(
                'INSERT requires "{rowid}" column. Missing in: {values}'.format(
                    rowid=self.rowid_column, values=new_values
                ),
                logging.ERROR,
            )
            return (0, 0)

        document_id = new_values[self.rowid_column]
        new_values.pop(self.rowid_column, None)

        for key in self.json_columns.intersection(new_values.keys()):
            new_values[key] = json.loads(new_values[key])

        try:
            response = self.client.index(
                id=document_id, body=new_values, **self.arguments
            )
            return response
        except Exception as exception:
            log2pg(
                "INDEX for {path}/{document_id} and document {document} failed: {exception}".format(
                    path=self.path,
                    document_id=document_id,
                    document=new_values,
                    exception=exception,
                ),
                logging.ERROR,
            )
            return (0, 0)

    def update(self, document_id, new_values):
        """ Update existing documents in Elastic Search """

        new_values.pop(self.rowid_column, None)

        for key in self.json_columns.intersection(new_values.keys()):
            new_values[key] = json.loads(new_values[key])

        try:
            response = self.client.index(
                id=document_id, body=new_values, **self.arguments
            )
            return response
        except Exception as exception:
            log2pg(
                "INDEX for {path}/{document_id} and document {document} failed: {exception}".format(
                    path=self.path,
                    document_id=document_id,
                    document=new_values,
                    exception=exception,
                ),
                logging.ERROR,
            )
            return (0, 0)

    def delete(self, document_id):
        """ Delete documents from Elastic Search """

        try:
            response = self.client.delete(id=document_id, **self.arguments)
            return response
        except Exception as exception:
            log2pg(
                "DELETE for {path}/{document_id} failed: {exception}".format(
                    path=self.path, document_id=document_id, exception=exception
                ),
                logging.ERROR,
            )
            return (0, 0)

    def _get_query(self, quals):
        if not self.query_column:
            return None

        return next(
            (
                qualifier.value
                for qualifier in quals
                if qualifier.field_name == self.query_column
            ),
            None,
        )

    def _get_sort(self, quals):
        if not self.sort_column:
            return self.default_sort

        return next(
            (
                qualifier.value
                for qualifier in quals
                if qualifier.field_name == self.sort_column and qualifier.value
            ),
            self.default_sort
        )

    def _convert_response_row(self, row_data, columns, query, sort):
        return_dict = {
            column: self._convert_response_column(column, row_data)
            for column in columns
            if column in row_data["_source"]
            or column == self.rowid_column
            or column == self.score_column
        }
        if query:
            return_dict[self.query_column] = query
        return_dict[self.sort_column] = sort
        return return_dict

    def _convert_response_column(self, column, row_data):
        if column == self.rowid_column:
            return row_data["_id"]
        if column == self.score_column:
            return row_data["_score"]
        value = row_data["_source"][column]
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value
