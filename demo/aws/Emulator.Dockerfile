FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG EMULATOR_VERSION=1.0.556
ARG RPM_ARCH=aarch64
ADD https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/aws-emulator-${EMULATOR_VERSION}-1-${RPM_ARCH}.rpm /tmp/aws-emulator.rpm
ADD https://downloads.aws.com/keys/gpg/current.asc /tmp/aws-publishing.asc
RUN rpm --import /tmp/aws-publishing.asc \
    && rpm --checksig /tmp/aws-emulator.rpm \
    && rpm -i /tmp/aws-emulator.rpm \
    && rm /tmp/aws-emulator.rpm /tmp/aws-publishing.asc
USER 1000
ENV HOME=/run/emulator
ENTRYPOINT ["aws-emulator"]
