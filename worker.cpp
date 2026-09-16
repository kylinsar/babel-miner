// SPDX-License-Identifier: MIT
// One-block legacy Keccak-256, packed bytes32 seed + address + uint256 nonce.
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <stdexcept>
#ifdef BABEL_CUDA
#include <cuda_runtime.h>
#define HD __host__ __device__
#define CK(x) do { auto e=(x); if(e!=cudaSuccess) throw std::runtime_error(cudaGetErrorString(e)); } while(0)
#else
#define HD
#endif
HD static uint64_t rol(uint64_t x, unsigned n) {return n ? (x<<n)|(x>>(64-n)) : x;}
HD static void permutation(uint64_t *a) {
 const uint64_t rc[24]={1ULL,0x8082ULL,0x800000000000808aULL,0x8000000080008000ULL,0x808bULL,0x80000001ULL,0x8000000080008081ULL,0x8000000000008009ULL,0x8aULL,0x88ULL,0x80008009ULL,0x8000000aULL,0x8000808bULL,0x800000000000008bULL,0x8000000000008089ULL,0x8000000000008003ULL,0x8000000000008002ULL,0x8000000000000080ULL,0x800aULL,0x800000008000000aULL,0x8000000080008081ULL,0x8000000000008080ULL,0x80000001ULL,0x8000000080008008ULL};
 const unsigned rot[25]={0,1,62,28,27,36,44,6,55,20,3,10,43,25,39,41,45,15,21,8,18,2,61,56,14};
 for(int r=0;r<24;r++) {
  uint64_t c[5],d[5],b[25];
  for(int x=0;x<5;x++) c[x]=a[x]^a[x+5]^a[x+10]^a[x+15]^a[x+20];
  for(int x=0;x<5;x++) d[x]=c[(x+4)%5]^rol(c[(x+1)%5],1);
  for(int y=0;y<5;y++) for(int x=0;x<5;x++) {int i=x+5*y; b[y+5*((2*x+3*y)%5)]=rol(a[i]^d[x],rot[i]);}
  for(int y=0;y<5;y++) for(int x=0;x<5;x++) a[x+5*y]=b[x+5*y]^((~b[(x+1)%5+5*y])&b[(x+2)%5+5*y]);
  a[0]^=rc[r];
 }
}
HD static void digest(const uint64_t* base,uint64_t counter,unsigned char* out) {
 uint64_t a[25]; for(int i=0;i<25;i++) a[i]=base[i];
 for(int i=0;i<8;i++) {int pos=76+i; a[pos/8]^=uint64_t((counter>>(56-8*i))&255)<<(8*(pos%8));}
 permutation(a);for(int i=0;i<32;i++) out[i]=(a[i/8]>>(8*(i%8)))&255;
}
HD static bool less(const unsigned char* a,const unsigned char* b) {for(int i=0;i<32;i++){if(a[i]!=b[i])return a[i]<b[i];}return false;}
static unsigned nib(char c){if(c>='0'&&c<='9')return c-'0';if(c>='a'&&c<='f')return c-'a'+10;if(c>='A'&&c<='F')return c-'A'+10;throw std::runtime_error("invalid hex");}
static void unhex(const std::string& s,unsigned char* b,size_t n){if(s.size()!=2*n)throw std::runtime_error("hex length");for(size_t i=0;i<n;i++)b[i]=(nib(s[2*i])<<4)|nib(s[2*i+1]);}
static std::string hex(const unsigned char* b,size_t n){const char* h="0123456789abcdef";std::string s;for(size_t i=0;i<n;i++){s+=h[b[i]>>4];s+=h[b[i]&15];}return s;}
#ifdef BABEL_CUDA
__global__ void search(const uint64_t* base,const unsigned char* target,uint64_t start,unsigned count,unsigned* win,unsigned char* last){
 unsigned i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=count)return;
 unsigned char h[32];digest(base,start+i,h);
 if(less(h,target))atomicMin(win,i);
 if(i==count-1)for(int j=0;j<32;j++)last[j]=h[j];
}
#endif
int main(int argc,char**argv){try{
#ifdef BABEL_CUDA
 if(argc!=2)throw std::runtime_error("GPU device index required"); CK(cudaSetDevice(std::stoi(argv[1])));
 uint64_t* db;unsigned char *dt,*dl;unsigned* dw;
 CK(cudaMalloc(&db,200));CK(cudaMalloc(&dt,32));CK(cudaMalloc(&dl,32));CK(cudaMalloc(&dw,4));
#endif
 std::string seed,sender,prefix,target;uint64_t start;unsigned count;
 while(std::cin>>seed>>sender>>prefix>>start>>count>>target){
  if(count==0||count>(1U<<26)||start>UINT64_MAX-(count-1))throw std::runtime_error("batch range invalid");
  unsigned char msg[136]={},t[32],h[32],nonce[32];uint64_t base[25]={};
  unhex(seed,msg,32);unhex(sender,msg+32,20);unhex(prefix,msg+52,24);unhex(target,t,32);
  msg[84]=1;msg[135]=128;for(int i=0;i<136;i++)base[i/8]|=uint64_t(msg[i])<<(8*(i%8));
  unsigned win=count-1,done=count;
#ifdef BABEL_CUDA
  unsigned sentinel=UINT32_MAX;
  CK(cudaMemcpy(db,base,200,cudaMemcpyHostToDevice));CK(cudaMemcpy(dt,t,32,cudaMemcpyHostToDevice));CK(cudaMemcpy(dw,&sentinel,4,cudaMemcpyHostToDevice));
  search<<<(count+255)/256,256>>>(db,dt,start,count,dw,dl);CK(cudaGetLastError());CK(cudaDeviceSynchronize());
  CK(cudaMemcpy(&sentinel,dw,4,cudaMemcpyDeviceToHost));
  if(sentinel!=UINT32_MAX){win=sentinel;digest(base,start+win,h);}else CK(cudaMemcpy(h,dl,32,cudaMemcpyDeviceToHost));
#else
  for(unsigned i=0;i<count;i++){digest(base,start+i,h);if(less(h,t)){win=i;done=i+1;break;}}
#endif
  unhex(prefix,nonce,24);uint64_t n=start+win;for(int i=0;i<8;i++)nonce[24+i]=(n>>(56-8*i))&255;
  std::cout<<done<<" "<<hex(nonce,32)<<" "<<hex(h,32)<<std::endl;
 }
 return 0;
 }catch(const std::exception& e){std::cerr<<"worker: "<<e.what()<<std::endl;return 1;}}
