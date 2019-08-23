# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""A script to deploy monitored projects.

Create a project config YAML file (see README.md for details) then run the
script with:
  bazel run :create_project -- \
    --project_yaml=my_project_config.yaml \
    --generated_fields_path=my_generated_fields.yaml \
    --projects='*' \
    --nodry_run \
    --alsologtostderr

To preview the commands that will run, use `--dry_run`.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import subprocess
import traceback

from absl import app
from absl import flags
from absl import logging

import yaml

from deploy.utils import forseti
from deploy.utils import runner
from deploy.utils import utils

FLAGS = flags.FLAGS

flags.DEFINE_string('project_yaml', None,
                    'Location of the project config YAML.')
flags.DEFINE_list('projects', ['*'],
                  ('Project IDs within --project_yaml to deploy, '
                   'or "*" to deploy all projects.'))
flags.DEFINE_string('generated_fields_path', None,
                    ('Path to save the YAML file containing post deployment '
                     'runtime fields. This file will be autogenerated.'))
flags.DEFINE_string('output_rules_path', None,
                    ('Path to local directory or GCS bucket to output rules '
                     'files. If unset, directly writes to the Forseti server '
                     'bucket.'))
flags.DEFINE_string(
    'apply_binary', None, 'Path to apply binary. '
    'Set automatically by the Bazel rule.')
flags.DEFINE_string(
    'apply_forseti_binary', None, 'Path to forseti installer. '
    'Set automatically by the Bazel rule.')
flags.DEFINE_string('rule_generator_binary', None,
                    ('Path to rule generator binary. '
                     'Set automatically by the Bazel rule.'))
flags.DEFINE_string('load_config_binary', None,
                    ('Path to load config binary. '
                     'Set automatically by the Bazel rule.'))
flags.DEFINE_string('grant_forseti_access_binary', None,
                    ('Path to the binary that grants forseti access. '
                     'Set automatically by the Bazel rule.'))
flags.DEFINE_bool('enable_terraform', False, 'DEV ONLY. Enable terraform.')

# Name of the Log Sink created in the data_project deployment manager template.
_LOG_SINK_NAME = 'audit-logs-to-bigquery'

# Restriction for project lien.
_LIEN_RESTRICTION = 'resourcemanager.projects.delete'

# Configuration for deploying a single project.
ProjectConfig = collections.namedtuple(
    'ProjectConfig',
    [
        # Dictionary of configuration values of the entire config.
        'root',
        # Dictionary of configuration values for this project.
        'project',
        # Dictionary of configuration values of the remote audit logs project,
        # or None if the project uses local logs.
        'audit_logs_project',
        # Extra steps to perform for this project.
        'extra_steps',
        # Dictionary of all generated fields.
        'generated_fields'
    ])

Step = collections.namedtuple(
    'Step',
    [
        # Function that implements this step.
        'func',
        # Description of the step.
        'description',
        # Whether this step should be run when updating a project.
        'updatable',
    ])


def get_or_create_new_project(config):
  """Creates the new GCP project if it does not exist."""
  project_id = config.project['project_id']

  # Attempt to get the project number first and fall back to creating the
  # project.
  # Note that it is possible that the `gcloud projects describe` command fails
  # due to reasons other than project does not exist (e.g. caller does not have
  # sufficient permission). In that case, project could exist and the code will
  # still attempt to create the project and fail if the project already exists.
  #
  # In the case where project exists, the organization_id / billing_account /
  # folder_id could be different from those specified in the config.
  # TODO: add check to enforce the metadata set in the config on the
  # existing project.
  try:
    config.generated_fields['projects'][project_id]['project_number'] = (
        utils.get_project_number(project_id))
    logging.info('Project %s exists, skip creating project.', project_id)
  except subprocess.CalledProcessError:
    overall_config = config.root['overall']
    org_id = overall_config.get('organization_id')
    folder_id = config.project.get('folder_id', overall_config.get('folder_id'))

    create_project_command = ['projects', 'create', project_id]
    if folder_id:
      create_project_command.extend(['--folder', folder_id])
    elif org_id:
      create_project_command.extend(['--organization', org_id])
    else:
      logging.info('Deploying without a parent organization or folder.')
    # Create the new project.
    runner.run_gcloud_command(create_project_command, project_id=None)
    config.generated_fields['projects'][project_id]['project_number'] = (
        utils.get_project_number(project_id))


def setup_billing(config):
  """Sets the billing account for this project."""
  billing_acct = config.project.get('billing_account',
                                    config.root['overall']['billing_account'])
  project_id = config.project['project_id']
  # Set the appropriate billing account for this project:
  runner.run_gcloud_command([
      'beta', 'billing', 'projects', 'link', project_id, '--billing-account',
      billing_acct
  ],
                            project_id=None)


def enable_services_apis(config):
  """Enables services for this project.

  Use this function instead of enabling private APIs in deployment manager
  because deployment-management does not have all the APIs' access, which might
  triger PERMISSION_DENIED errors.

  Args:
    config (ProjectConfig): The config of a single project to setup.

  Returns:
    List[string]: commands to remove APIs not found in the enabled set.
  """
  project_id = config.project['project_id']

  want_apis = set(config.project.get('enabled_apis', []))
  want_apis.add('deploymentmanager.googleapis.com')
  # For project level iam policy updates.
  want_apis.add('cloudresourcemanager.googleapis.com')

  # TODO  long term solution for updating APIs.
  resources = config.project.get('resources', {})
  if 'gce_instances' in resources:
    want_apis.add('compute.googleapis.com')
  if 'iam_policies' in resources or 'iam_custom_roles' in resources:
    want_apis.add('iam.googleapis.com')
  if 'chc_datasets' in resources:
    want_apis.add('healthcare.googleapis.com')
  if 'gke_clusters' in resources:
    want_apis.add('container.googleapis.com')

  want_apis = list(want_apis)

  # Send in batches to avoid hitting quota limits.
  for i in range(0, len(want_apis), 10):
    runner.run_gcloud_command(
        ['services', 'enable'] + want_apis[i:i + 10], project_id=project_id)


def get_data_bucket_name(data_bucket, project_id):
  """Get the name of data buckets."""
  if 'name' not in data_bucket:
    if 'name_suffix' not in data_bucket:
      raise utils.InvalidConfigError(
          'Data buckets must contains either name or name_suffix')
    return project_id + data_bucket['name_suffix']
  else:
    if 'name_suffix' in data_bucket:
      raise utils.InvalidConfigError(
          'Data buckets must not contains both name and name_suffix')
    return data_bucket['name']


def create_deletion_lien(config):
  """Create the project deletion lien, if specified."""
  # Create project liens if requested.
  if not config.project.get('create_deletion_lien'):
    return
  project_id = config.project['project_id']
  existing_restrictions = runner.run_gcloud_command(
      [
          'alpha', 'resource-manager', 'liens', 'list', '--format',
          'value(restrictions)'
      ],
      project_id=project_id).split('\n')

  if _LIEN_RESTRICTION not in existing_restrictions:
    runner.run_gcloud_command([
        'alpha', 'resource-manager', 'liens', 'create', '--restrictions',
        _LIEN_RESTRICTION, '--reason',
        'Automated project deletion lien deployment.'
    ],
                              project_id=project_id)


def deploy_resources(config):
  """Deploy resources."""
  utils.call_go_binary([
      FLAGS.apply_binary,
      '--project_yaml_path',
      FLAGS.project_yaml,
      '--generated_fields_path',
      FLAGS.generated_fields_path,
      '--project',
      config.project['project_id'],
      '--enable_terraform=%s' % FLAGS.enable_terraform,
  ])


def create_compute_images(config):
  """Creates new Compute Engine VM images if specified in config.

  Note: for updates, only new images will be created. Existing images will not
  be modified.

  Args:
    config (ProjectConfig): config of the project.
  """
  gce_instances = config.project.get('gce_instances', [])
  gce_instances.extend(
      config.project.get('resources', {}).get('gce_instances', []))
  if not gce_instances:
    logging.info('No GCS Images required.')
    return
  project_id = config.project['project_id']

  for instance in gce_instances:
    custom_image = instance.get('custom_boot_image')
    if not custom_image:
      logging.info('Using existing image')
      continue
    # Check if custom image already exists.
    if runner.run_gcloud_command([
        'compute', 'images', 'list', '--no-standard-images', '--filter',
        'name={}'.format(custom_image['image_name']), '--format', 'value(name)'
    ],
                                 project_id=project_id):
      logging.info('Image %s already exists, skipping image creation.',
                   custom_image['image_name'])
      continue
    logging.info('Creating VM Image %s.', custom_image['image_name'])

    # Create VM image using gcloud rather than deployment manager so that the
    # deployment manager service account doesn't need to be granted access to
    # the image GCS bucket.
    image_uri = 'gs://' + custom_image['gcs_path']
    runner.run_gcloud_command([
        'compute', 'images', 'create', custom_image['image_name'],
        '--source-uri', image_uri
    ],
                              project_id=project_id)


def create_stackdriver_account(config):
  """Prompts the user to create a new Stackdriver Account."""
  # Creating a Stackdriver account cannot be done automatically, so ask the
  # user to create one.
  if 'stackdriver_alert_email' not in config.project:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver account.')
    return
  project_id = config.project['project_id']

  if _stackdriver_account_exists(project_id):
    logging.info('Stackdriver account already exists')
    return

  message = """
  ------------------------------------------------------------------------------
  To create email alerts, this project needs a Stackdriver account.
  Create a new Stackdriver account for this project by visiting:
      https://console.cloud.google.com/monitoring?project={}

  Only add this project, and skip steps for adding additional GCP or AWS
  projects. You don't need to install Stackdriver Agents.

  IMPORTANT: Wait about 5 minutes for the account to be created.

  For more information, see: https://cloud.google.com/monitoring/accounts/

  After the account is created, enter [Y] to continue, or enter [N] to skip the
  creation of Stackdriver alerts.
  ------------------------------------------------------------------------------
  """.format(project_id)
  print(message)

  # Keep trying until Stackdriver account is ready, or user skips.
  while True:
    if not utils.wait_for_yes_no('Account created [y/N]?'):
      logging.warning('Skipping creation of Stackdriver Account.')
      break

    if _stackdriver_account_exists(project_id):
      break


def _stackdriver_account_exists(project_id):
  """Determine whether the stackdriver account exists."""
  try:
    runner.run_gcloud_command(['alpha', 'monitoring', 'policies', 'list'],
                              project_id=project_id)
    return True
  except subprocess.CalledProcessError as e:
    logging.warning(
        'Error reading Stackdriver account (likely does not exist): %s', e)
    return False


def create_alerts(config):
  """"Creates Stackdriver alerts for logs-based metrics."""
  # Stackdriver alerts can't yet be created in Deployment Manager, so create
  # them here.
  alert_email = config.project.get('stackdriver_alert_email')
  if alert_email is None:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver alerts.')
    return
  project_id = config.project['project_id']

  existing_channels_str = runner.run_gcloud_command([
      'alpha', 'monitoring', 'channels', 'list', '--format',
      'value(name,labels.email_address)'
  ],
                                                    project_id=project_id)

  existing_channels = existing_channels_str.split(
      '\n') if existing_channels_str else []

  email_to_channel = {}
  for existing_channel in existing_channels:
    channel, email = existing_channel.split()

    # assume only one channel exists per email
    email_to_channel[email] = channel

  if alert_email in email_to_channel:
    logging.info('Stackdriver notification channel already exists for %s',
                 alert_email)
    channel = email_to_channel[alert_email]
  else:
    logging.info('Creating Stackdriver notification channel.')
    channel = utils.create_notification_channel(alert_email, project_id)

  existing_alerts = runner.run_gcloud_command([
      'alpha', 'monitoring', 'policies', 'list', '--format',
      'value(displayName)'
  ],
                                              project_id=project_id).split('\n')

  existing_alerts = set(existing_alerts)

  logging.info('Creating Stackdriver alerts.')
  display_name = 'IAM Policy Change Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['global', 'pubsub_topic', 'pubsub_subscription', 'gce_instance'],
        'iam-policy-change-count', display_name,
        ('This policy ensures the designated user/group is notified when IAM '
         'policies are altered.'), channel, project_id)

  display_name = 'Bucket Permission Change Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['gcs_bucket'], 'bucket-permission-change-count', display_name,
        ('This policy ensures the designated user/group is notified when '
         'bucket/object permissions are altered.'), channel, project_id)

  display_name = 'Bigquery Update Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['global'], 'bigquery-settings-change-count', display_name,
        ('This policy ensures the designated user/group is notified when '
         'Bigquery dataset settings are altered.'), channel, project_id)

  buckets = []
  for bucket in config.project.get('data_buckets', []):
    # Every bucket with 'expected_users' has an expected-access alert.
    if 'expected_users' not in bucket:
      continue
    bucket_name = get_data_bucket_name(bucket, project_id)
    buckets.append(bucket_name)

  for bucket in config.project.get('resources', {}).get('gcs_buckets', []):
    # Every bucket with 'expected_users' has an expected-access alert.
    if 'expected_users' not in bucket:
      continue
    if 'name' not in bucket['properties']:
      raise utils.InvalidConfigError('GCS bucket must contains name')
    buckets.append(bucket['properties']['name'])

  for bucket in buckets:
    metric_name = 'unexpected-access-' + bucket
    display_name = 'Unexpected Access to {} Alert'.format(bucket)
    if display_name not in existing_alerts:
      utils.create_alert_policy(
          ['gcs_bucket'], metric_name, display_name,
          ('This policy ensures the designated user/group is notified when '
           'bucket {} is accessed by an unexpected user.'.format(bucket)),
          channel, project_id)


def add_project_generated_fields(config):
  """Adds a generated_fields block to a project definition."""
  project_id = config.project['project_id']
  generated_fields = config.generated_fields['projects'][project_id]
  generated_fields[
      'log_sink_service_account'] = utils.get_log_sink_service_account(
          _LOG_SINK_NAME, project_id)

  if 'gce_instances' not in config.project.get('resources', {}):
    generated_fields.pop('gce_instance_info', None)
    return

  gce_instance_info = utils.get_gce_instance_info(project_id)
  if gce_instance_info:
    generated_fields['gce_instance_info'] = gce_instance_info


# The steps to set up a project, so the script can be resumed part way through
# on error. Each func takes a config dictionary.
_SETUP_STEPS = [
    Step(
        func=get_or_create_new_project,
        description='Get or create project',
        updatable=False,
    ),
    Step(
        func=setup_billing,
        description='Set up billing',
        updatable=False,
    ),
    Step(
        func=enable_services_apis,
        description='Enable APIs',
        updatable=True,
    ),
    Step(
        func=create_compute_images,
        description='Deploy compute images',
        updatable=True,
    ),
    Step(
        func=create_deletion_lien,
        description='Create deletion lien',
        updatable=True,
    ),
    Step(
        func=deploy_resources,
        description='Deploy resources',
        updatable=True,
    ),
    Step(
        func=create_stackdriver_account,
        description='Create Stackdriver account',
        updatable=True,
    ),
    Step(
        func=create_alerts,
        description='Create Stackdriver alerts',
        updatable=True,
    ),
    Step(
        func=add_project_generated_fields,
        description='Generate project fields',
        updatable=True,
    ),
]


def setup_project(config):
  """Run the full process for initalizing a single new project.

  Note: for projects that have already been deployed, only the updatable steps
  will be run.

  Args:
    config (ProjectConfig): The config of a single project to setup.

  Returns:
    A boolean, true if the project was deployed successfully, false otherwise.
  """
  project_id = config.project['project_id']
  steps = _SETUP_STEPS + config.extra_steps

  project_generated_fields = config.generated_fields['projects'].get(project_id)
  if not project_generated_fields:
    project_generated_fields = {}
    config.generated_fields['projects'][project_id] = project_generated_fields
    deployed = False
  else:
    deployed = 'failed_step' not in project_generated_fields

  starting_step = project_generated_fields.get('failed_step', 1)

  total_steps = len(steps)
  for step_num in range(starting_step, total_steps + 1):
    step = steps[step_num - 1]
    project_id = config.project['project_id']
    logging.info('%s: step %d/%d (%s)', project_id, step_num, total_steps,
                 step.description)

    if deployed and not step.updatable:
      logging.info('Step %d is not updatable, skipping', step_num)
      continue

    try:
      step.func(config)
    except Exception as e:  # pylint: disable=broad-except
      traceback.print_exc()
      logging.error('%s: setup failed on step %s: %s', project_id, step_num, e)
      logging.error('Failure information has been written to --project_yaml.')

      # only record failed step if project was undeployed, an update can always
      # start from the beginning
      if not deployed:
        project_generated_fields['failed_step'] = step_num
        write_generated_fields(config)
        logging.info(
            'Failure info has been written to the generated fields block. '
            'Please correct any issues and re-run the script.')

      return False

    write_generated_fields(config)

  # if this deployment was resuming from a previous failure, remove the
  # failed step as it is done
  project_generated_fields.pop('failed_step', None)
  write_generated_fields(config)
  logging.info('Setup completed successfully.')

  return True


def install_forseti(config):
  """Install forseti based on the given config."""
  utils.call_go_binary([
      FLAGS.apply_forseti_binary,
      '--project_yaml_path',
      FLAGS.project_yaml,
      '--generated_fields_path',
      FLAGS.generated_fields_path,
      '--enable_remote_state=%s' % FLAGS.enable_terraform,
  ])
  forseti_config = config.root['forseti']
  forseti_project_id = forseti_config['project']['project_id']
  generated_fields = {
      'service_account': forseti.get_server_service_account(forseti_project_id),
      'server_bucket': forseti.get_server_bucket(forseti_project_id)
  }
  config.generated_fields['forseti'] = generated_fields


def get_forseti_access_granter_step(project_id):
  """Get step to grant access to the forseti instance for the project."""

  def grant_access(config):
    utils.call_go_binary([
        FLAGS.grant_forseti_access_binary,
        '--project_id',
        project_id,
        '--forseti_service_account',
        config.generated_fields['forseti']['service_account'],
    ])

  return Step(
      func=grant_access,
      description='Grant Access to Forseti Service account',
      updatable=False,
  )


def validate_project_configs(overall, projects):
  """Check if the configurations of projects are valid.

  Args:
    overall (dict): The overall configuration of all projects.
    projects (list): A list of dictionaries of projects.
  """
  if 'allowed_apis' not in overall:
    return

  allowed_apis = set(overall['allowed_apis'])
  missing_allowed_apis = collections.defaultdict(list)
  for project in projects:
    for api in project.project.get('enabled_apis', []):
      if api not in allowed_apis:
        missing_allowed_apis[api].append(project.project['project_id'])
  if missing_allowed_apis:
    raise utils.InvalidConfigError(
        ('Projects try to enable the following APIs '
         'that are not in the allowed_apis list:\n%s' % missing_allowed_apis))


def write_generated_fields(config):
  """Write gen fields to --generated_fields_path."""
  utils.write_yaml_file(config.generated_fields, FLAGS.generated_fields_path)


def main(argv):
  del argv  # Unused.

  if FLAGS.generated_fields_path == FLAGS.project_yaml:
    raise Exception(
        '--generated_fields_path must not be set to the same as --project_yaml.'
    )

  if FLAGS.output_rules_path:
    FLAGS.output_rules_path = utils.normalize_path(FLAGS.output_rules_path)

  FLAGS.project_yaml = utils.normalize_path(FLAGS.project_yaml)
  FLAGS.generated_fields_path = utils.normalize_path(
      FLAGS.generated_fields_path)

  # touch file if it has not been created yet
  open(FLAGS.generated_fields_path, 'a').close()

  config_string = runner.run_command([
      FLAGS.load_config_binary,
      '--project_yaml_path',
      FLAGS.project_yaml,
      '--generated_fields_path',
      FLAGS.generated_fields_path,
  ],
                                     get_output=True)
  root_config = yaml.load(config_string)

  if not root_config:
    logging.error('Error loading project YAML.')
    return

  generated_fields = utils.read_yaml_file(FLAGS.generated_fields_path)
  if not generated_fields:
    generated_fields = {'projects': {}}

  want_projects = set(FLAGS.projects)

  def want_project(project_config_dict):
    if not project_config_dict:
      return False

    return want_projects == {
        '*'
    } or project_config_dict['project_id'] in want_projects

  projects = []
  audit_logs_project = root_config.get('audit_logs_project')

  # Always deploy the remote audit logs project first (if present).
  if want_project(audit_logs_project):
    projects.append(
        ProjectConfig(
            root=root_config,
            project=audit_logs_project,
            audit_logs_project=None,
            extra_steps=[],
            generated_fields=generated_fields))

  forseti_config = root_config.get('forseti')

  if forseti_config and want_project(forseti_config['project']):
    extra_steps = [
        Step(
            func=install_forseti,
            description='Install Forseti',
            updatable=False,
        ),
        get_forseti_access_granter_step(
            forseti_config['project']['project_id']),
    ]

    if audit_logs_project:
      extra_steps.append(
          get_forseti_access_granter_step(audit_logs_project['project_id']))

    forseti_project_config = ProjectConfig(
        root=root_config,
        project=forseti_config['project'],
        audit_logs_project=audit_logs_project,
        extra_steps=extra_steps,
        generated_fields=generated_fields)
    projects.append(forseti_project_config)

  for project_config in root_config.get('projects', []):
    if not want_project(project_config):
      continue

    extra_steps = []
    if forseti_config:
      extra_steps.append(
          get_forseti_access_granter_step(project_config['project_id']))

    projects.append(
        ProjectConfig(
            root=root_config,
            project=project_config,
            audit_logs_project=audit_logs_project,
            extra_steps=extra_steps,
            generated_fields=generated_fields))

  validate_project_configs(root_config['overall'], projects)

  logging.info('Found %d projects to deploy', len(projects))

  for config in projects:
    logging.info('Setting up project %s', config.project['project_id'])

    if not setup_project(config):
      # Don't attempt to deploy additional projects if one project failed.
      return

  if forseti_config:
    call = [
        FLAGS.rule_generator_binary,
        '--project_yaml_path',
        FLAGS.project_yaml,
        '--generated_fields_path',
        FLAGS.generated_fields_path,
        '--output_path',
        FLAGS.output_rules_path or '',
    ]
    logging.info('Running rule generator: %s', call)
    utils.call_go_binary(call)

  logging.info('All projects successfully deployed.')


if __name__ == '__main__':
  flags.mark_flag_as_required('project_yaml')
  flags.mark_flag_as_required('generated_fields_path')
  flags.mark_flag_as_required('apply_binary')
  flags.mark_flag_as_required('apply_forseti_binary')
  flags.mark_flag_as_required('rule_generator_binary')
  flags.mark_flag_as_required('load_config_binary')
  flags.mark_flag_as_required('grant_forseti_access_binary')
  app.run(main)
