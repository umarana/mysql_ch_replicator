import json
import os.path
import time
import pickle
from logging import getLogger
from enum import Enum
from dataclasses import dataclass
from collections import defaultdict

from config import Settings, MysqlSettings, ClickhouseSettings
from mysql_api import MySQLApi
from clickhouse_api import ClickhouseApi
from converter import MysqlToClickhouseConverter
from table_structure import TableStructure
from binlog_replicator import DataReader, LogEvent, EventType


logger = getLogger(__name__)


class Status(Enum):
    NONE = 0
    CREATING_INITIAL_STRUCTURES = 1
    PERFORMING_INITIAL_REPLICATION = 2
    RUNNING_REALTIME_REPLICATION = 3


class State:

    def __init__(self, file_name):
        self.file_name = file_name
        self.last_processed_transaction = None
        self.last_processed_transaction_non_uploaded = None
        self.status = Status.NONE
        self.tables_last_record_version = {}
        self.initial_replication_table = None
        self.initial_replication_max_primary_key = None
        self.tables_structure: dict[str, tuple[TableStructure, TableStructure]] = {}
        self.tables = []
        self.load()

    def load(self):
        file_name = self.file_name
        if not os.path.exists(file_name):
            return
        data = open(file_name, 'rb').read()
        data = pickle.loads(data)
        self.last_processed_transaction = data['last_processed_transaction']
        self.last_processed_transaction_non_uploaded = data['last_processed_transaction']
        self.status = Status(data['status'])
        self.tables_last_record_version = data['tables_last_record_version']
        self.initial_replication_table = data['initial_replication_table']
        self.initial_replication_max_primary_key = data['initial_replication_max_primary_key']
        self.tables_structure = data['tables_structure']
        self.tables = data['tables']

    def save(self):
        file_name = self.file_name
        data = pickle.dumps({
            'last_processed_transaction': self.last_processed_transaction,
            'status': self.status.value,
            'tables_last_record_version': self.tables_last_record_version,
            'initial_replication_table': self.initial_replication_table,
            'initial_replication_max_primary_key': self.initial_replication_max_primary_key,
            'tables_structure': self.tables_structure,
            'tables': self.tables,
        })
        with open(file_name + '.tmp', 'wb') as f:
            f.write(data)
        os.rename(file_name + '.tmp', file_name)


@dataclass
class Statistics:
    last_transaction: tuple[str, int] | None = None
    events_count: int = 0
    insert_events_count: int = 0
    insert_records_count: int = 0
    erase_events_count: int = 0
    erase_records_count: int = 0


class DbReplicator:

    INITIAL_REPLICATION_BATCH_SIZE = 50000
    SAVE_STATE_INTERVAL = 10
    STATS_DUMP_INTERVAL = 60

    DATA_DUMP_INTERVAL = 1
    DATA_DUMP_BATCH_SIZE = 10000

    READ_LOG_INTERVAL = 1

    def __init__(self, config: Settings, database: str):
        self.config = config
        self.database = database
        self.mysql_api = MySQLApi(
            database=database,
            mysql_settings=config.mysql,
        )
        self.clickhouse_api = ClickhouseApi(
            database=database,
            clickhouse_settings=config.clickhouse,
        )
        self.converter = MysqlToClickhouseConverter(self)
        self.data_reader = DataReader(config.binlog_replicator, database)
        self.state = State(os.path.join(config.binlog_replicator.data_dir, database, 'state.pckl'))
        self.clickhouse_api.tables_last_record_version = self.state.tables_last_record_version
        self.last_save_state_time = 0
        self.stats = Statistics()
        self.last_dump_stats_time = 0
        self.records_to_insert = defaultdict(dict)  # table_name => {record_id=>record, ...}
        self.records_to_delete = defaultdict(set)  # table_name => {record_id, ...}
        self.last_records_upload_time = 0

    def run(self):
        if self.state.status == Status.RUNNING_REALTIME_REPLICATION:
            self.run_realtime_replication()
            return
        if self.state.status == Status.PERFORMING_INITIAL_REPLICATION:
            self.perform_initial_replication()
            self.run_realtime_replication()
            return

        logger.info('recreating database')
        self.clickhouse_api.recreate_database()
        self.state.tables = self.mysql_api.get_tables()
        self.state.last_processed_transaction = self.data_reader.get_last_transaction_id()
        self.state.save()
        logger.info(f'last known transaction {self.state.last_processed_transaction}')
        self.create_initial_structure()
        self.perform_initial_replication()
        self.run_realtime_replication()

    def create_initial_structure(self):
        self.state.status = Status.CREATING_INITIAL_STRUCTURES
        for table in self.state.tables:
            self.create_initial_structure_table(table)
        self.state.save()

    def create_initial_structure_table(self, table_name):
        mysql_create_statement = self.mysql_api.get_table_create_statement(table_name)
        mysql_structure = self.converter.parse_mysql_table_structure(
            mysql_create_statement, required_table_name=table_name,
        )
        clickhouse_structure = self.converter.convert_table_structure(mysql_structure)
        self.state.tables_structure[table_name] = (mysql_structure, clickhouse_structure)
        self.clickhouse_api.create_table(clickhouse_structure)

    def perform_initial_replication(self):
        logger.info('running initial replication')
        self.state.status = Status.PERFORMING_INITIAL_REPLICATION
        self.state.save()
        start_table = self.state.initial_replication_table
        for table in self.state.tables:
            if start_table and table != start_table:
                continue
            self.perform_initial_replication_table(table)
            start_table = None

    def perform_initial_replication_table(self, table_name):
        logger.info(f'running initial replication for table {table_name}')

        max_primary_key = None
        if self.state.initial_replication_table == table_name:
            # continue replication from saved position
            max_primary_key = self.state.initial_replication_max_primary_key
            logger.info(f'continue from primary key {max_primary_key}')
        else:
            # starting replication from zero
            logger.info(f'replicating from scratch')
            self.state.initial_replication_table = table_name
            self.state.initial_replication_max_primary_key = None
            self.state.save()

        mysql_table_structure, clickhouse_table_structure = self.state.tables_structure[table_name]
        field_names = [field.name for field in clickhouse_table_structure.fields]
        field_types = [field.field_type for field in clickhouse_table_structure.fields]

        primary_key = clickhouse_table_structure.primary_key
        primary_key_index = field_names.index(primary_key)
        primary_key_type = field_types[primary_key_index]

        while True:

            query_start_value = max_primary_key
            if 'Int' not in primary_key_type:
                query_start_value = f"'{query_start_value}'"

            records = self.mysql_api.get_records(
                table_name=table_name,
                order_by=primary_key,
                limit=DbReplicator.INITIAL_REPLICATION_BATCH_SIZE,
                start_value=query_start_value,
            )

            records = self.converter.convert_records(records, mysql_table_structure, clickhouse_table_structure)

            # for record in records:
            #     print(dict(zip(field_names, record)))

            if not records:
                break
            self.clickhouse_api.insert(table_name, records)
            for record in records:
                record_primary_key = record[primary_key_index]
                if max_primary_key is None:
                    max_primary_key = record_primary_key
                else:
                    max_primary_key = max(max_primary_key, record_primary_key)

            self.state.initial_replication_max_primary_key = max_primary_key
            self.save_state_if_required()

    def run_realtime_replication(self):
        self.mysql_api.close()
        self.mysql_api = None
        logger.info(f'running realtime replication from the position: {self.state.last_processed_transaction}')
        self.state.status = Status.RUNNING_REALTIME_REPLICATION
        self.state.save()
        self.data_reader.set_position(self.state.last_processed_transaction)
        while True:
            event = self.data_reader.read_next_event()
            if event is None:
                time.sleep(DbReplicator.READ_LOG_INTERVAL)
                self.upload_records_if_required(table_name=None)
                continue
            self.handle_event(event)

    def handle_event(self, event: LogEvent):
        if self.state.last_processed_transaction_non_uploaded is not None:
            if event.transaction_id <= self.state.last_processed_transaction_non_uploaded:
                return

        logger.debug(f'processing event {event.transaction_id}')
        self.stats.events_count += 1
        self.stats.last_transaction = event.transaction_id
        self.state.last_processed_transaction_non_uploaded = event.transaction_id

        event_handlers = {
            EventType.ADD_EVENT.value: self.handle_insert_event,
            EventType.REMOVE_EVENT.value: self.handle_erase_event,
            EventType.QUERY.value: self.handle_query_event,
        }

        event_handlers[event.event_type](event)

        self.upload_records_if_required(table_name=event.table_name)

        self.save_state_if_required()
        self.log_stats_if_required()

    def save_state_if_required(self):
        curr_time = time.time()
        if curr_time - self.last_save_state_time < DbReplicator.SAVE_STATE_INTERVAL:
            return
        self.last_save_state_time = curr_time
        self.state.tables_last_record_version = self.clickhouse_api.tables_last_record_version
        self.state.save()

    def handle_insert_event(self, event: LogEvent):
        self.stats.insert_events_count += 1
        self.stats.insert_records_count += len(event.records)

        mysql_table_structure = self.state.tables_structure[event.table_name][0]
        clickhouse_table_structure = self.state.tables_structure[event.table_name][1]
        records = self.converter.convert_records(event.records, mysql_table_structure, clickhouse_table_structure)

        primary_key_ids = mysql_table_structure.primary_key_idx

        current_table_records_to_insert = self.records_to_insert[event.table_name]
        current_table_records_to_delete = self.records_to_delete[event.table_name]
        for record in records:
            record_id = record[primary_key_ids]
            current_table_records_to_insert[record_id] = record
            current_table_records_to_delete.discard(record_id)

    def handle_erase_event(self, event: LogEvent):
        self.stats.erase_events_count += 1
        self.stats.erase_records_count += len(event.records)

        table_structure: TableStructure = self.state.tables_structure[event.table_name][0]
        table_structure_ch: TableStructure = self.state.tables_structure[event.table_name][1]

        primary_key_name_idx = table_structure.primary_key_idx
        field_type_ch = table_structure_ch.fields[primary_key_name_idx].field_type

        if field_type_ch == 'String':
            keys_to_remove = [f"'{record[primary_key_name_idx]}'" for record in event.records]
        else:
            keys_to_remove = [record[primary_key_name_idx] for record in event.records]

        current_table_records_to_insert = self.records_to_insert[event.table_name]
        current_table_records_to_delete = self.records_to_delete[event.table_name]
        for record_id in keys_to_remove:
            current_table_records_to_delete.add(record_id)
            current_table_records_to_insert.pop(record_id, None)

    def handle_query_event(self, event: LogEvent):
        query = event.records.strip()
        print(" === handle_query_event", query)
        if query.lower().startswith('alter'):
            self.handle_alter_query(query, event.db_name)
        if query.lower().startswith('create table'):
            self.handle_create_table_query(query, event.db_name)
        if query.lower().startswith('drop table'):
            self.handle_drop_table_query(query, event.db_name)


    def handle_alter_query(self, query, db_name):
        self.upload_records()
        ch_alter_query = self.converter.convert_alter_query(query, db_name)
        if ch_alter_query is None:
            print('skip query', query)
            return
        self.clickhouse_api.execute_command(ch_alter_query)

    def handle_create_table_query(self, query, db_name):
        mysql_structure, ch_structure = self.converter.parse_create_table_query(query)
        self.state.tables_structure[mysql_structure.table_name] = (mysql_structure, ch_structure)
        self.clickhouse_api.create_table(ch_structure)

    def handle_drop_table_query(self, query):
        pass

    def log_stats_if_required(self):
        curr_time = time.time()
        if curr_time - self.last_dump_stats_time < DbReplicator.STATS_DUMP_INTERVAL:
            return
        self.last_dump_stats_time = curr_time
        logger.info(f'statistics:\n{json.dumps(self.stats.__dict__, indent=3)}')
        self.stats = Statistics()

    def upload_records_if_required(self, table_name):
        need_dump = False
        if table_name is not None:
            if len(self.records_to_insert[table_name]) >= DbReplicator.DATA_DUMP_BATCH_SIZE:
                need_dump = True
            if len(self.records_to_delete[table_name]) >= DbReplicator.DATA_DUMP_BATCH_SIZE:
                need_dump = True

        curr_time = time.time()
        if curr_time - self.last_records_upload_time >= DbReplicator.DATA_DUMP_INTERVAL:
            need_dump = True

        if not need_dump:
            return

        self.upload_records()

    def upload_records(self):
        self.last_records_upload_time = time.time()

        for table_name, id_to_records in self.records_to_insert.items():
            records = id_to_records.values()
            if not records:
                continue
            self.clickhouse_api.insert(table_name, records)

        for table_name, keys_to_remove in self.records_to_delete.items():
            if not keys_to_remove:
                continue
            table_structure: TableStructure = self.state.tables_structure[table_name][0]
            primary_key_name = table_structure.primary_key
            self.clickhouse_api.erase(
                table_name=table_name,
                field_name=primary_key_name,
                field_values=keys_to_remove,
            )

        self.records_to_insert = defaultdict(dict)  # table_name => {record_id=>record, ...}
        self.records_to_delete = defaultdict(set)  # table_name => {record_id, ...}
        self.state.last_processed_transaction = self.state.last_processed_transaction_non_uploaded
        self.save_state_if_required()
