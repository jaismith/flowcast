import { useEffect } from 'react';
import { useRouter } from 'next/router';
import { Center, Container, Space, Text, Title } from '@mantine/core'

const Redirect404 = () => {
  const router = useRouter();

  useEffect(() => {
    const timer = setTimeout(() => {
      router.replace('/sites/01427510');
    }, 1500);

    return () => clearTimeout(timer);
  }, [router]);

  return (
    <Center style={{ height: '100vh', flexDirection: 'column' }}>
      <Container style={{ textAlign: 'center' }}>
        <Title order={1}>404 - Page Not Found</Title>
        <Text style={{ marginTop: 20 }}>Redirecting...</Text>
      </Container>
    </Center>
  );
};

export default Redirect404;
