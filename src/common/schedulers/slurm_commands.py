# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.


import logging
import math
from textwrap import wrap

from common.schedulers.converters import ComparableObject, from_table_to_obj_list
from common.utils import check_command_output

PENDING_RESOURCES_REASONS = [
    "Resources",
    "Nodes required for job are DOWN, DRAINED or reserved for jobs in higher priority partitions",
    "BeginTime",
    "NodeDown",
    "Priority",
    "ReqNodeNotAvail, May be reserved for other job",
]

SQUEUE_FIELD_SIZE = 200
_SQUEUE_FIELDS = [
    "jobid",
    "statecompact",
    "numnodes",
    "numcpus",
    "numtasks",
    "cpus-per-task",
    "mincpus",
    "reason",
    "tres-per-job",
    "tres-per-task",
    "tres-per-node",
]
SQUEUE_FIELD_STRING = ",".join([field + ":{size}" for field in _SQUEUE_FIELDS]).format(size=SQUEUE_FIELD_SIZE)


def get_jobs_info(job_state_filter=None):
    """
    Retrieve the list of submitted jobs.

    :param job_state_filter: filter jobs by the given state
    :return: a list of SlurmJob objects representing the submitted jobs.
    """
    command = "/opt/slurm/bin/squeue -r -O '{0}'".format(SQUEUE_FIELD_STRING)
    if job_state_filter:
        command += " --states {0}".format(job_state_filter)

    output = check_command_output(command)
    return SlurmJob.from_table(output)


def get_pending_jobs_info(
    max_slots_filter=None, max_nodes_filter=None, filter_by_pending_reasons=None, max_gpus_per_node=0
):
    """
    Retrieve the list of pending jobs from the Slurm scheduler.

    The list is filtered based on the args passed to the function.
    Moreover the number of required nodes is recomputed by taking into account the number of
    CPUs required by each task and the number of nodes requested by the user. See _recompute_required_nodes_per_job
    for additional details.

    :param max_slots_filter: max number of slots in a compute node.
    :param max_nodes_filter: max number of nodes in the cluster.
    :param filter_by_pending_reasons: retrieve only jobs with the following pending reasons.
    :return: array of filtered SlurmJobs
    """
    pending_jobs = get_jobs_info(job_state_filter="PD")
    if max_slots_filter:
        _recompute_required_nodes_by_slots_reservation(pending_jobs, max_slots_filter)
        _recompute_required_nodes_by_gpu_reservation(pending_jobs, max_gpus_per_node)
    if max_slots_filter or filter_by_pending_reasons or max_nodes_filter:
        filtered_jobs = []
        for job in pending_jobs:
            if max_slots_filter and job.cpus_min_per_node > max_slots_filter:
                logging.info(
                    "Skipping job %s since required slots per node (%d) exceed max slots (%d)",
                    job.id,
                    job.cpus_min_per_node,
                    max_slots_filter,
                )
            elif max_nodes_filter and job.nodes > max_nodes_filter:
                logging.info(
                    "Skipping job %s since required nodes (%d) exceed max nodes in cluster (%d)",
                    job.id,
                    job.nodes,
                    max_nodes_filter,
                )
            elif filter_by_pending_reasons and job.pending_reason not in filter_by_pending_reasons:
                logging.info("Skipping pending job %s due to pending reason: %s", job.id, job.pending_reason)
            else:
                filtered_jobs.append(job)

        return filtered_jobs
    else:
        return pending_jobs


def _recompute_required_nodes_by_slots_reservation(pending_jobs, node_slots):
    """
    Adjust the number of required nodes if necessary based on the required slots.

    According to squeue manual page:
    %D    Number of nodes allocated to the job or the minimum number of nodes required by a pending job.
    The actual number of nodes allocated to a pending job may exceed this number if the job specified a node range count
    (e.g.  minimum and maximum node counts) or the job specifies a processor count instead of a node count and the
    cluster contains nodes with varying processor counts.

    For example, when submitting a job with: sbatch -c 3 -n 5 in a cluster with 4-slot nodes, the output of the squeue
    command is:
        /opt/slurm/bin/squeue -r -o '%i|%t|%D|%C|%c|%r'
        JOBID|ST|NODES|CPUS|MIN_CPUS|REASON
        91|PD|4|15|3|Nodes required for job are DOWN, DRAINED or reserved for jobs in higher priority partitions

    The following function, based on the number of slots required per task, computes the necessary amount of nodes
    to run the job by considering the number of tasks that can fit on a single node.

    :param pending_jobs: array of SlurmJob
    :param node_slots: max slots per compute node
    """
    for job in pending_jobs:
        if node_slots >= job.cpus_min_per_node:
            # check the max number of slots I can fill with tasks of size cpus_min_per_node
            usable_slots_per_node = node_slots - (node_slots % job.cpus_min_per_node)
            # compute number of nodes by considering nodes of size usable_slots_per_node
            required_nodes = int(math.ceil(float(job.cpus_total) / usable_slots_per_node))
            # setting nodes to the max between the computed required nodes and the number of nodes given by
            # the squeue output. This is done to handle job submissions with the -N option where the user can
            # specify a number of nodes that is bigger than the min required.
            # e.g. sbatch -c 1 -N 2 -n 2 => (cpus_min_per_node=1, cpus_total=2, nodes=2)
            job.nodes = max(required_nodes, job.nodes)


def _recompute_required_nodes_by_gpu_reservation(pending_jobs, gpus_per_node):
    """
    Adjust the number of required nodes if necessary based on the required GPUs.

    When using --gpus option or --gpus-per-task without explicitly specifying the number of nodes Slurm will not
    display the correct number of required nodes in the squeue output. The actual number needs to be recomputed
    based on TRES_PER_JOB and TRES_PER_TASK data.

    See examples below (Note output has been compressed):
    ubuntu@ip-10-0-0-64:~$ sbatch --wrap "sleep 100" --gpus=12
    Submitted batch job 79
    ubuntu@ip-10-0-0-64:~$ /opt/slurm/bin/squeue -r -O 'jobid,statecompact,numnodes,numcpus,cpus-per-task,reason,
        tres-per-job,tres-per-task'
    JOBID|ST|NODES|CPUS|CPUS_PER_TASK|REASON|TRES_PER_JOB|TRES_PER_TASK
    79|PD|1|1|1|ReqNodeNotAvail, Maygpu:12|N/A
    ubuntu@ip-10-0-0-64:~$ sbatch --wrap "sleep 100" --gpus-per-task=2 -n 3
    Submitted batch job 92
    ubuntu@ip-10-0-0-64:~$ /opt/slurm/bin/squeue -r -O 'jobid,statecompact,numnodes,numcpus,cpus-per-task,reason,
        tres-per-job,tres-per-task'
    JOBID|ST|NODES|CPUS|CPUS_PER_TASK|REASON|TRES_PER_JOB|TRES_PER_TASK
    92|PD|1|3|1|ReqNodeNotAvail, May|N/A|gpu:2

    :param pending_jobs: array of SlurmJob
    :param gpus_per_node: max gpus per compute node
    """
    if gpus_per_node <= 0:
        # do not process any filtering since we assume scheduler rejects all jobs that require GPUs
        return

    for job in pending_jobs:
        gpus_per_job = job.tres_per_job.get("gpu")
        if gpus_per_job:
            new_min_nodes = int(math.ceil(float(gpus_per_job) / gpus_per_node))
            if new_min_nodes > job.nodes:
                job.nodes = new_min_nodes
                if job.cpus_total < job.nodes * job.cpus_min_per_node:
                    job.cpus_total = job.nodes * job.cpus_min_per_node

        gpus_per_task = job.tres_per_task.get("gpu")
        if gpus_per_task:
            tasks_schedulable_per_node = float(gpus_per_node) // gpus_per_task
            new_min_nodes = int(math.ceil(float(job.tasks) / tasks_schedulable_per_node))
            if new_min_nodes > job.nodes:
                job.nodes = new_min_nodes
                if job.cpus_total < job.nodes * job.cpus_min_per_node:
                    job.cpus_total = job.nodes * job.cpus_min_per_node


def transform_tres_to_dict(value):
    if value == "N/A":
        return {}

    tres_dict = {}
    for tres in value.split(","):
        resource, value = tres.split(":")
        tres_dict[resource] = int(value)
    return tres_dict


class SlurmJob(ComparableObject):
    # This is the format after being processed by reformat_table function
    # JOBID|ST|NODES|CPUS|TASKS|CPUS_PER_TASK|MIN_CPUS|REASON|TRES_PER_JOB|TRES_PER_TASK
    # 72|PD|2|5|5|1|1|Resources|N/A|N/A
    # 86|PD|10|40|4|4|4|PartitionConfig|gpu:12|N/A
    # 87|PD|10|10|10|1|1|PartitionNodeLimit|N/A|gpu:4
    MAPPINGS = {
        "JOBID": {"field": "id"},
        "ST": {"field": "state"},
        "NODES": {"field": "nodes", "transformation": int},
        "CPUS": {"field": "cpus_total", "transformation": int},
        "TASKS": {"field": "tasks", "transformation": int},
        "CPUS_PER_TASK": {"field": "cpus_per_task", "transformation": int},
        "MIN_CPUS": {"field": "cpus_min_per_node", "transformation": int},
        "REASON": {"field": "pending_reason"},
        "TRES_PER_JOB": {"field": "tres_per_job", "transformation": transform_tres_to_dict},
        "TRES_PER_TASK": {"field": "tres_per_task", "transformation": transform_tres_to_dict},
        "TRES_PER_NODE": {"field": "tres_per_node", "transformation": transform_tres_to_dict},
    }

    def __init__(
        self,
        id=None,
        state="",
        nodes=0,
        cpus_total=0,
        tasks=0,
        cpus_per_task=0,
        cpus_min_per_node=0,
        pending_reason="",
        tres_per_job=None,
        tres_per_task=None,
        tres_per_node=None,
    ):
        self.id = id
        self.state = state
        self.nodes = nodes
        self.cpus_total = cpus_total
        self.tasks = tasks
        self.cpus_per_task = cpus_per_task
        self.cpus_min_per_node = cpus_min_per_node
        self.pending_reason = pending_reason
        self.tres_per_job = tres_per_job or {}
        self.tres_per_task = tres_per_task or {}
        self.tres_per_node = tres_per_node or {}

    @staticmethod
    def reformat_table(table):
        """
        Reformat the output of squeue command.

        The -O option used with squeue only supports fixed width formatting and is not as flexible as -o.
        This function removes all empty spaces and compresses the table in a format that is suitable for
        from_table_to_obj_list function
        :param table: the output of squeue -O command
        :return: the compressed table with "|" used as delimiter
        """
        lines = table.splitlines()
        for i in range(0, len(lines)):
            lines[i] = "|".join(wrap(lines[i], SQUEUE_FIELD_SIZE))
        return "\n".join(lines)

    @staticmethod
    def from_table(table):
        return from_table_to_obj_list(SlurmJob.reformat_table(table), SlurmJob)
