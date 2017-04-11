import argparse
import gzip
import logging
import os
import re

import boto.s3
import warc
from gzipstream import GzipStreamFile

from pyspark import SparkContext, SparkConf
from pyspark.sql import SQLContext
from pyspark.sql.types import StructType, StructField, StringType, LongType


LOGGING_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


class CCSparkJob:

    name = 'CCSparkJob'

    output_schema = StructType([
        StructField("key", StringType(), True),
        StructField("val", LongType(), True)
    ])

    args = None
    records_processed = None
    log_level = 'INFO'
    logging.basicConfig(level=log_level, format=LOGGING_FORMAT)

    num_input_partitions = 400
    num_output_partitions = 10

    def parse_arguments(self):
        """ Returns the parsed arguments from the command line """
        arg_parser = argparse.ArgumentParser(description=self.name)

        arg_parser.add_argument("input",
                                help="Path to file listing input paths")
        arg_parser.add_argument("output",
                                help="Output path (subdir of spark.sql.warehouse.dir)")

        arg_parser.add_argument("--num_input_partitions", type=int,
                                default=self.num_input_partitions,
                                help="number of input splits/partitions")
        arg_parser.add_argument("--num_output_partitions", type=int,
                                default=self.num_output_partitions,
                                help="number of output partitions")

        arg_parser.add_argument("--log_level", default=self.log_level,
                                help="Logging level")

        self.add_arguments(arg_parser)
        args = arg_parser.parse_args()
        self.validate_arguments(args)
        self.init_logging(args.log_level)

        return args

    def add_arguments(self, parser):
        pass

    def validate_arguments(self, args):
        return True

    def init_logging(self, level=None):
        if level is None:
            level = self.log_level
        else:
            self.log_level = level
        logging.basicConfig(level=level, format=LOGGING_FORMAT)

    def get_logger(self, spark_context=None):
        """Get logger from SparkContext or (if None) from logging module"""
        if spark_context is None:
            return logging.getLogger(self.name)
        return spark_context._jvm.org.apache.log4j.LogManager \
            .getLogger(self.name)

    def run(self):
        self.args = self.parse_arguments()

        conf = SparkConf().setAll((
            ("spark.task.maxFailures", "10"),
            ("spark.locality.wait", "20s"),
            ("spark.serializer", "org.apache.spark.serializer.KryoSerializer"),
        ))
        sc = SparkContext(
            appName=self.name,
            conf=conf)
        sqlc = SQLContext(sparkContext=sc)

        self.records_processed = sc.accumulator(0)

        self.run_job(sc, sqlc)

        sc.stop()

    def run_job(self, sc, sqlc):
        input_data = sc.textFile(self.args.input,
                                 minPartitions=self.args.num_input_partitions)

        output = input_data.mapPartitionsWithIndex(self.process_warcs) \
            .reduceByKey(lambda x, y: x + y)

        sqlc.createDataFrame(output, schema=self.output_schema) \
            .coalesce(self.args.num_output_partitions) \
            .write \
            .format("parquet") \
            .saveAsTable(self.args.output)

        self.get_logger(sc).info('records processed = {}'.format(
            self.records_processed.value))

    def process_warcs(self, id_, iterator):
        s3conn = None
        ccbucket = None
        s3pattern = re.compile('^s3://([^/]+)/(.+)')
        base_dir = os.path.abspath(os.path.dirname(__file__))

        for uri in iterator:
            if uri.startswith('s3://'):
                self.get_logger().info('Reading from S3 {}'.format(uri))
                if s3conn is None:
                    s3conn = boto.connect_s3(anon=True,
                                             host='s3.amazonaws.com')
                    ccbucket = s3conn.get_bucket('commoncrawl')
                s3match = s3pattern.match(uri)
                if s3match is None:
                    self.get_logger().error("Invalid S3 URI: " + uri)
                bucketname = s3match.group(1)
                path = s3match.group(2)
                if bucketname == 'commoncrawl':
                    bucket = ccbucket
                else:
                    bucket = s3conn.get_bucket(bucketname)
                s3key = boto.s3.key.Key(bucket, path)
                stream = warc.WARCFile(fileobj=GzipStreamFile(s3key))
            elif uri.startswith('hdfs://'):
                self.get_logger().error("HDFS input not implemented: " + uri)
                continue
            else:
                self.get_logger().info('Reading local stream {}'.format(uri))
                if uri.startswith('file:'):
                    uri = uri[5:]
                uri = os.path.join(base_dir, uri)
                stream = warc.WARCFile(fileobj=gzip.open(uri))

            for record in stream:
                for res in self.process_record(record):
                    yield res
                self.records_processed.add(1)

    def process_record(self, record):
        raise NotImplementedError('Processing record needs to be customized')